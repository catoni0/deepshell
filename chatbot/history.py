import os
import re
import asyncio
import aiofiles
import numpy as np
from utils.logger import Logger
from config.settings import Mode
from chatbot.helper import PromptHelper
from chatbot.deployer import ChatBotDeployer
from ollama_client.api_client import OllamaClient
from sklearn.metrics.pairwise import cosine_similarity


logger = Logger.get_logger()

helper, filter_helper = ChatBotDeployer.deploy_chatbot(Mode.HELPER)

class Topic:
    def __init__(self, name="", description="") -> None:
        """
        Initializes a Topic with a name and description.
        The description is embedded and cached for matching.
        
        Args:
            name (str): The topic name.
            description (str): A textual description of the topic.
        """
        self.name = name
        self.description = description
        self.embedded_description = np.array([])
        self.history: list[dict[str, str]] = []
        self.history_embeddings = [] 
        self.file_embeddings: dict[str, dict] = {}  # Stores file info as dictionary (name, full path, embedding)
        self.embedding_cache: dict[str, np.ndarray] = {}
        self.folder_structure: dict = {}

    async def add_message(self, role, message, embedding):
        """Stores raw messages and their embeddings."""
        self.history.append({"role": role, "content": message})
        self.history_embeddings.append(embedding)
        logger.info(f"Message added to {self.name}")

    async def get_relevant_context(self, embedding) -> tuple[float, int]:
        """
        Retrieves the best similarity score and the index of the most relevant message
        from the topic’s history based on cosine similarity.

        Args:
            query_embedding.
            similarity_threshold (float): The base similarity threshold.

        Returns:
            tuple[float, int]: A tuple containing:
                - The best similarity score.
                - The index of the best matching message (or -1 if not found).
        """
        if not self.history_embeddings:
            logger.info("No history embeddings found. Returning empty context.")
            return 0.0, -1

        similarities = cosine_similarity([embedding], self.history_embeddings)[0]
        best_index = int(np.argmax(similarities))
        best_similarity = float(similarities[best_index])
        logger.debug(f"Best similarity score: {best_similarity} at index {best_index}")
        
        return best_similarity, best_index


    def _index_file(self, file_path: str, embedding):
        """
        Internal method to add a file's embedding (along with file name and path) to the topic.
        
        Args:
            file_path (str): The file's path.
            content (str): The file content.
            embedding (np.ndarray): Pre-computed embedding of the content.
        """
        file_name = os.path.basename(file_path)
        file_info = {
            "file_name": file_name,
            "full_path": file_path,
            "embedding": embedding
        }
       
        # Add the file information to file_embeddings
        self.file_embeddings[file_path] = file_info
        logger.debug(f"Topic '{self.name}': Added file {file_path}")

 
class HistoryManager:
    def __init__(self,manager, top_k: int = 2, similarity_threshold: float = 0.5) -> None:
        """
        Initializes HistoryManager to handle topics and off-topic tracking.
        An "unsorted" topic collects messages and files until a clear topic emerges.
        
        Args:
            top_k (int): Maximum number of context items to retrieve.
            similarity_threshold (float): Threshold for determining similarity.
        """
        self.manager = manager
        self.top_k = top_k
        self.similarity_threshold = similarity_threshold
        self.topics: list[Topic] = []
        self.current_topic = Topic()
        self.embedding_cache: dict[str, np.ndarray] = {}
        self.projects =  {}


    async def add_message(self, role, message,embedding = None) -> None:
        """
        Routes a new message to the best-matching topic.
        If no topic meets the similarity threshold, the message is added to the unsorted topic.

        Args:
            role (str): Sender's role.
            message (str): The message text.
        """
        if embedding:
            embedding = await self.fetch_embedding(message)
        topic = await self._match_topic(embedding, exclude_topic = self.current_topic)
        if topic:
            await self.switch_topic(topic) 

        await self.current_topic.add_message(role, message,embedding)
        asyncio.create_task(self._analyze_history())


    async def _read_file(self, file_path: str) -> tuple[str, str]:
        """
        Asynchronously reads a file.
        
        Args:
            file_path (str): The file's path.
        
        Returns:
            tuple[str, str]: The file path and its content.
        """
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = await f.read()
            return file_path, content
        except (IOError, OSError) as e:
            logger.error(f" Error reading file {file_path}: {str(e)}")
            return file_path, ""


    async def switch_topic(self,topic):
        async with asyncio.Lock():
            if topic.name != self.current_topic.name:
                if not any(t.name == self.current_topic.name for t in self.topics):
                    self.topics.append(self.current_topic)
                logger.info(f"Switched to {topic.name}")
                self.current_topic = topic

    
    async def _match_topic(self, embedding, exclude_topic: Topic | None = None) -> Topic | None:
        """
        Matches a message or file embedding to the most similar topic based on the description embedding,
        optionally excluding a specified topic.

        Args:
            embedding (np.ndarray): The embedding to match.
            exclude_topic (Topic | None): A topic to exclude from matching (e.g. the current topic).

        Returns:
            Topic | None: The best matching topic if similarity exceeds the threshold.
        """
        # Early exit if no topics are available.
        if len(self.topics) == 0:
            logger.info("No topics available for matching. Returning None.")
            return None

        async def compute_similarity(topic: Topic) -> tuple[float, Topic]:
            if len(topic.embedded_description) == 0 or len(embedding) == 0:
                return 0.0, topic
            similarity = cosine_similarity([embedding], [topic.embedded_description])[0][0]
            logger.debug(f"Computed similarity {similarity:.4f} for topic '{topic.name}'")
            return similarity, topic

        # Exclude the specified topic from matching.
        tasks = [compute_similarity(topic) for topic in self.topics if topic != exclude_topic]
        results = await asyncio.gather(*tasks)
        
        best_topic = None
        best_similarity = 0.0
        for similarity, topic in results:
            if similarity > best_similarity and similarity >= self.similarity_threshold:
                best_similarity = similarity
                best_topic = topic

        if best_topic:
            logger.info(f"Best matching topic: '{best_topic.name}' with similarity {best_similarity:.4f}")
            return best_topic
        else:
            logger.info("No suitable topic found.")
            return None
        


   
    async def add_file(self, file_path: str, content: str) -> None:
        """
        Adds a file by computing its combined embedding (file path + content) 
        and routing it to the best matching topic.
        The file is added to the unsorted topic.

        Args:
            file_path (str): The file's path. 
            content (str): The file content.

        """

        combined_content = f"Path: {file_path}\nContent: {content}"
        file_embedding = await self.fetch_embedding(combined_content)  # Ensure embedding function is async
        self.current_topic._index_file(file_path, file_embedding)


    def add_folder_structure(self, structure,topic_name= None) -> None:
        """
        Adds or updates folder structure.
        If topic_name is provided, the folder structure is applied to that topic;
        otherwise, it is applied to the unsorted topic.
        
        Args:
            structure (dict): The folder structure.
            topic_name (str | None): Optional topic name.
        """
        if topic_name:
            topic_name.folder_structure = structure                
            logger.info(f"Folder structure updated for topic: {topic_name}")
            return

        logger.warning(f"Topic '{topic_name}' not found. Applying folder structure to unsorted topic.")
        self.current_topic.folder_structure = structure
        logger.info("Folder structure updated for unsorted topic.")



    def find_project_structure(self, query):
        """
        Checks if the query contains a folder name and retrieves the stored structure if found.

        Args:
            query (str): The user query.
        
        Returns:
            dict | None: The folder structure if found, else None.
        """
        for project_name in self.projects:
            if project_name.lower() in query.lower():
                logger.info(f"Found project structure for '{project_name}' in query")
                return self.projects[project_name]
        
        logger.info("No matching project found in query")
        return None
    

    def extract_file_name_from_query(self,query: str):
        # Regex to capture common file name patterns (with or without full path)
        file_pattern = r"([a-zA-Z0-9_\-]+(?:/[a-zA-Z0-9_\-]+)*/[a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+|[a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)"
        match = re.search(file_pattern, query)
        if match:
            return match.group(0)
        return None 
        
    
 
    async def get_relevant_files(self, query: str, top_k: int = 1, similarity_threshold: float = 0.3):
        """
        Retrieves relevant files by performing cosine similarity between query and file embeddings.
        The file embeddings are pre-computed with content, metadata, and file path combined.
        """
        # Extract potential file name or path from the query
        file_name = self.extract_file_name_from_query(query)

        # Always compute the query embedding (even if no file name is found)
        query_embedding = await self.fetch_embedding(query)

        # Step 1: If a file name was extracted, attempt to match it first
        file_scores = []
        if file_name:
            # Look for exact matches in current topic first
            for file_path, file_info in self.current_topic.file_embeddings.items():
                # Check if the file name is in the extracted query file name
                if file_name.lower() in file_info['file_name'].lower():
                    file_scores.append((file_path, 1.0))  # Full match

            # If no exact match found, compute similarity based on embedding
            if not file_scores:
                for file_path, file_info in self.current_topic.file_embeddings.items():
                    similarity = cosine_similarity([query_embedding], [file_info['embedding']])[0][0]
                    if similarity >= similarity_threshold:
                        file_scores.append((file_path, similarity))

        # Step 2: If no relevant files found, expand the search across all topics
        if not file_scores:
            logger.info("No relevant files found in the current topic, expanding search across all topics.")
            
            all_file_embeddings = {}
            for topic in self.topics:
                all_file_embeddings.update(topic.file_embeddings)

            for file_path, file_info in all_file_embeddings.items():
                similarity = cosine_similarity([query_embedding], [file_info['embedding']])[0][0]
                if similarity >= similarity_threshold:
                    file_scores.append((file_path, similarity))

        # If we have file matches, sort by similarity and return the top_k files
        if file_scores:
            file_scores.sort(key=lambda x: x[1], reverse=True)
            selected_file_paths = [fp for fp, _ in file_scores[:top_k]]
            tasks = [self._read_file(fp) for fp in selected_file_paths]
            results = await asyncio.gather(*tasks)
            return [(fp, content) for fp, content in results if content]

        logger.info("No matching file embeddings found")
        return None

    async def fetch_embedding(self, text: str) -> np.ndarray: 
        """
        Asynchronously fetches and caches an embedding for the given text.
        Uses async lock to guard the caching mechanism.
        """
        async with asyncio.Lock():
            # If the embedding is cached, return it
            if text in self.embedding_cache:
                return self.embedding_cache[text]

        embedding = await OllamaClient.fetch_embedding(text)
        if embedding:
            self.embedding_cache[text] = embedding
            logger.debug(f"Extracted {len(embedding)} embeddings")
            return embedding
        else:
            return np.array([])
       
    
    
    async def generate_prompt(self, query, num_messages=5):
        """
        Generates a prompt by retrieving context and file references from the best matching topic.
        If the query references a folder, the corresponding folder structure is retrieved and assigned
        to the current topic before being included in the prompt.

        Args:
            query (str): The user query.
        
        Returns:
            list: The last few messages from the topic's history.
        """
        # Compute embedding for the query
        embedding = await self.fetch_embedding(query)
        
        # Determine the best matching topic and switch to it if found
        current_topic = await self._match_topic(embedding)
        if current_topic:
            await self.switch_topic(current_topic)
        
        # Retrieve the project folder structure if the query contains a folder name/path.
        # This method should search your projects dictionary for a matching folder.
        project_structure = self.find_project_structure(query)
        if project_structure:
            # Assign the retrieved structure to the current topic
            self.current_topic.folder_structure = project_structure

        # Retrieve relevant files (using your existing logic that compares the combined embeddings)
        relevant_files = await self.get_relevant_files(query)
        file_references = ""
        
        # If a folder structure is present, format it and include it in the prompt.
        if self.current_topic.folder_structure:
            file_references += f"Folder structure:\n{self.format_structure(self.current_topic.folder_structure)}\n"
        
        # Append file references if relevant files were found.
        if relevant_files:
            for file_path, content in relevant_files:
                file_references += f"\n[Referenced File: {file_path}]\n{content}\n..."
        
        # Combine the query with any file references to form the prompt.
        prompt = f"{query}\n\n{file_references}" if file_references else query
        
        logger.info(f"Generated prompt: {prompt}")
        await self.add_message("user", prompt, embedding)
        
        return self.current_topic.history[-num_messages:]


    def format_structure(self, folder_structure):
        """
        Formats the folder structure dictionary into a readable string format.
        """
        def format_substructure(substructure, indent=0):
            formatted = ""
            for key, value in substructure.items():
                if isinstance(value, dict):  # Subfolder
                    formatted += " " * indent + f"{key}/\n"
                    formatted += format_substructure(value, indent + 4)
                else:  # File
                    formatted += " " * indent + f"-- {value}\n"
            return formatted
        
        return format_substructure(folder_structure)


    async def generate_topic_info_from_history(self,history, max_retries: int = 3):
        """
        Attempts to extract a topic name and description from the given history.
        
        Args:
            history (list): List of unsorted history messages.
            max_retries (int): Maximum number of attempts.
            
        Returns:
            tuple: (extracted_topic_name, extracted_topic_description) if successful; otherwise (None, None).
        """
        attempt = 0
        extracted_topic_name = None
        extracted_topic_description = None

        while attempt < max_retries:
            try:
                response = await helper._fetch_response(PromptHelper.topics_helper(history))
                response = response.strip("`").strip("json")
                if not response:
                    raise ValueError("Received empty response from the helper.")
                
                await filter_helper.process_static(response)
                response = helper.last_response
                if not response:
                    raise ValueError("Response empty after filtering.")

                logger.debug(f"Extracting topic info from response: {response}")
                clean_response = re.sub(r"^```|```$|json", "", response, flags=re.IGNORECASE).strip()
                matches = re.findall(r':\s*"([^"]+)"', clean_response)
                extracted_topic_name = matches[0] if len(matches) > 0 else "unknown"
                extracted_topic_description = matches[1] if len(matches) > 1 else ""
                
                if extracted_topic_name and extracted_topic_description:
                    logger.info(f"Extracted topic: {extracted_topic_name}")
                    return extracted_topic_name, extracted_topic_description
                else:
                    raise ValueError("Could not extract valid topic information.")
            except Exception as e:
                logger.error(f"Analyze history attempt {attempt + 1} failed: {str(e)}", exc_info=True)
                attempt += 1
                if attempt < max_retries:
                    logger.info(f"Retrying analysis... (Attempt {attempt + 1} of {max_retries})")
                else:
                    logger.warning("Max analysis retries reached; not splitting unsorted history.")
                    break
        return None, None

 
    
    async def _analyze_history(
        self,
        off_topic_threshold: float = 0.7,
        off_topic_frequency: int = 4,
        slice_size: int = 4
    ) -> None:
        """
        Analyzes the current topic's history for potential off-topic drift.
        When the history length reaches a multiple of `off_topic_frequency`, the method:
          1. Takes a slice of the last `slice_size` messages and computes per-message similarity to the current topic.
          2. If more than half of the messages in the slice have a similarity below `off_topic_threshold`,
             it determines the precise start of the off-topic segment.
          3. Generates candidate topic info for the off-topic segment and attempts to match it with an existing topic
             (excluding the current topic). If a match is found, the off-topic messages are reassigned; otherwise,
             a new topic is created.
          4. Finally, the off-topic messages are removed from the current topic.
        """
        # If the current topic is unnamed but has > 4 messages, generate a topic name/description.
        if len(self.current_topic.history) > 4 and not self.current_topic.name.strip():
            new_topic_name, new_topic_desc = await self.generate_topic_info_from_history(self.current_topic.history)
            if new_topic_name and new_topic_desc:
                self.current_topic.name = new_topic_name
                self.current_topic.description = new_topic_desc
                self.current_topic.embedded_description = await self.fetch_embedding(new_topic_desc)
                return

        # Trigger analysis when history length is a multiple of off_topic_frequency.
        if (len(self.current_topic.history) >= off_topic_frequency and 
            len(self.current_topic.history) % off_topic_frequency == 0):
            logger.info("Analyzing current topic for potential off-topic segments.")

            current_name = self.current_topic.name

            # Candidate slice: the last `slice_size` messages.
            candidate_slice = self.current_topic.history[-slice_size:]
            
            # Concurrently fetch embeddings for the candidate slice.
            candidate_embeddings = await asyncio.gather(
                *(self.fetch_embedding(msg["content"]) for msg in candidate_slice)
            )
            
            similarities = []
            for msg_emb in candidate_embeddings:
                
                sim = cosine_similarity([msg_emb], [self.current_topic.embedded_description])[0][0]
                similarities.append(sim)
            logger.info(f"Per-message similarities for candidate slice: {similarities}")

            # Check if more than half of the messages fall below the threshold.
            if sum(1 for s in similarities if s < off_topic_threshold) > len(similarities) / 2:
                # Identify the precise start index of the off-topic segment.
                off_topic_start_index = len(self.current_topic.history) - slice_size
                for i, sim in enumerate(similarities):
                    if sim < off_topic_threshold:
                        off_topic_start_index = len(self.current_topic.history) - slice_size + i
                        break
                off_topic_segment = self.current_topic.history[off_topic_start_index:]
                logger.info(f"Identified off-topic segment from index {off_topic_start_index} to end "
                            f"(total {len(off_topic_segment)} messages).")
               
                # Generate candidate topic info from the off-topic segment.
                candidate_topic_name, candidate_topic_desc = await self.generate_topic_info_from_history(off_topic_segment)
                if candidate_topic_name and candidate_topic_desc:
                    candidate_embedding = await self.fetch_embedding(candidate_topic_desc)
                    matched_topic = await self._match_topic(candidate_embedding, exclude_topic=self.current_topic)
                    if matched_topic is not None:
                        logger.info("Matched topic found")
                        # Reassign off-topic messages to the matched topic.
                        for msg in off_topic_segment:
                            msg_emb = await self.fetch_embedding(msg["content"])
                            
                            await matched_topic.add_message(msg["role"], msg["content"], msg_emb)
                        logger.info(f"Reassigned off-topic segment of {len(off_topic_segment)} messages to existing topic "
                                    f"'{matched_topic.name}'.")
                        await self.switch_topic(matched_topic)

                    else:
                        # No matching topic found—create a new topic.
                        try:
                            logger.info("Creating new topic from the off-topic content")
                            new_topic = Topic(candidate_topic_name, candidate_topic_desc)
                            new_topic.embedded_description = candidate_embedding
                            for msg in off_topic_segment:
                                msg_emb = await self.fetch_embedding(msg["content"])
                                
                                await new_topic.add_message(msg["role"], msg["content"], msg_emb)

                            await self.switch_topic(new_topic)

                     
                            logger.info(f"Created new topic '{new_topic.name}' with {len(off_topic_segment)} off-topic messages.")

                        except Exception as e:
                            logger.error(f"Error creating new topic from off-topic messages: {e}", exc_info=True)

                        target_topic = next((topic for topic in self.topics if topic.name == current_name), None)
                        if target_topic:
                            async with asyncio.Lock():
                                target_topic.history = target_topic.history[:off_topic_start_index]
                                logger.info("Removed off-topic from the current topic")                            

                else:
                    logger.warning("Could not generate candidate topic info from the off-topic segment.")
            else:
                logger.info("Candidate slice does not appear off-topic; no splitting performed.")

        return   
