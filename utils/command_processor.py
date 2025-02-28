import os
import re
from utils.logger import Logger
from utils.file_utils import FileUtils
from utils.shell_utils import CommandExecutor

logger = Logger.get_logger()

class CommandProcessor:
    """Handles user input"""
    
    def __init__(self, manager):
        self.manager = manager
        self.ui = manager.ui
        self.file_utils = FileUtils(manager)
        self.executor = CommandExecutor(self.ui)
    
    async def handle_command(self, user_input):
        """Processes commands, handles file/folder operations, and updates config."""
        bypass_flag = False

        if user_input.startswith("!"):
            user_input = user_input[1:] 
            bypass_flag = True
            return user_input, bypass_flag

        if user_input:
            target, additional_action = await self.detect_action(user_input)
            if target:
                await self.file_utils.process_file_or_folder(target)
                if additional_action:
                    user_input = additional_action
                   
                return user_input
            else:
                if additional_action == "cancel":
                    return None

        return user_input

   
    async def detect_action(self, user_input):
        """Detects action, validates/finds target, and processes file/folder."""

        # Extract action and target
        parts = re.split(r"\band\b", user_input, maxsplit=1)
        main_command = parts[0].strip()
        additional_action = parts[1].strip() if len(parts) > 1 else None
        actions = {"find", "open", "read"}
        tokens = main_command.split()

        if not tokens:
            return None, None

        # Ensure the action is at the beginning of the input
        action = tokens[0] if tokens[0] in actions else None
        if not action:
            return None, None

        # Extract target (everything after the action)
        target_index = 1  # Start from the second token, assuming it's the target
        target = " ".join(tokens[target_index:]) if target_index < len(tokens) else ""

        # Convert "this folder" to current working directory
        if target.lower() == "this folder":
            target = os.getcwd()

        # Validate or find target
        target = target.strip()
        if not os.path.exists(target):
            choice = await self.file_utils.prompt_search(target)
            if not choice:
                if self.ui:
                    await self.ui.fancy_print("\n[cyan]System:[/cyan] Nothing found\n")
                return None, None
            if choice == "cancel":
                if self.ui:
                    await self.ui.fancy_print("\n[cyan]System:[/cyan] search canceled by user\n")
                return choice
            target = choice

        # Ensure default additional action if none is specified
        if target:
            if not additional_action:
                additional_action = f"Analyze the content of {target}"
            else:
                additional_action = f"{additional_action} for {target}"

        return target, additional_action

    def format_input(self, user_input, file_content, additional_action=None):
        """Prepares user input by combining prompt and file content."""
        formatted_content = f"Content:\n{file_content}"
        if additional_action:
            user_input = additional_action
        if user_input:
            return f"\n{formatted_content}\nUser Prompt:\n{user_input}\n"
        return formatted_content
