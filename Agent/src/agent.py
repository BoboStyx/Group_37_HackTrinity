"""
Main AI agent implementation coordinating between different models and tasks.
"""
from typing import Optional, Dict, Any, List
import logging
from datetime import datetime, timedelta

from chatgpt_agent import ChatGPTAgent
from o3_mini import O3MiniAgent
from database import SessionLocal, Conversation, AgentTask, Task, get_tasks_by_urgency, update_task_status
from config import (
    MAX_RETRIES, TIMEOUT, MAX_TOKENS, MAX_EMAILS,
    URGENCY_ORDER, HALF_FINISHED_PRIORITY
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class AIAgent:
    def __init__(self):
        """Initialize the AI agent with its component models."""
        self.chatgpt = ChatGPTAgent()
        self.o3_mini = O3MiniAgent()
        self.db = None  # Initialize as None
        try:
            self.db = SessionLocal()
        except Exception as e:
            logger.error(f"Failed to initialize database: {str(e)}")

        # Log availability of models
        if not self.chatgpt.is_available:
            logger.warning("ChatGPT model is not available")
        if not self.o3_mini.is_available:
            logger.warning("O3-mini model is not available")

    async def process_input(self, user_input: str, context: Optional[Dict[str, Any]] = None) -> str:
        """
        Process user input and return the appropriate response.
        
        Args:
            user_input: The user's input text
            context: Optional context dictionary for maintaining conversation state
                    Special keys:
                    - is_greeting: True if this is the initial greeting
                    - has_tasks: Whether there are any tasks
                    - task_count: Number of current tasks
                    - history: List of previous message exchanges
        
        Returns:
            str: The agent's response
        
        Raises:
            RuntimeError: If no models are available
        """
        if not self.chatgpt.is_available and not self.o3_mini.is_available:
            raise RuntimeError(
                "No AI models are available. Please check your API keys in the .env file."
            )

        try:
            # Log the incoming request
            logger.info(f"Processing user input: {user_input[:100]}...")

            # Create a new task
            task = AgentTask(
                task_type="process_input",
                status="in_progress"
            )
            self.db.add(task)
            self.db.commit()

            # Determine which model to use
            use_o3_mini = (
                self.o3_mini.is_available and 
                self._requires_deep_thinking(user_input)
            )

            response_chunks = []
            try:
                if use_o3_mini:
                    async for chunk in self.o3_mini.process(user_input, context):
                        response_chunks.append(chunk)
                    model_used = "o3-mini"
                else:
                    if not self.chatgpt.is_available:
                        raise RuntimeError("ChatGPT is not available and this input requires it")
                    async for chunk in self.chatgpt.process(user_input, context):
                        response_chunks.append(chunk)
                    model_used = "gpt-4"

            except Exception as model_error:
                # If primary model fails, try fallback to the other model
                logger.warning(f"Primary model failed: {str(model_error)}")
                response_chunks = []
                if use_o3_mini and self.chatgpt.is_available:
                    logger.info("Falling back to ChatGPT")
                    async for chunk in self.chatgpt.process(user_input, context):
                        response_chunks.append(chunk)
                    model_used = "gpt-4"
                elif not use_o3_mini and self.o3_mini.is_available:
                    logger.info("Falling back to O3-mini")
                    async for chunk in self.o3_mini.process(user_input, context):
                        response_chunks.append(chunk)
                    model_used = "o3-mini"
                else:
                    raise

            response = "".join(response_chunks)

            # Store the conversation
            conversation = Conversation(
                user_input=user_input,
                agent_response=response,
                model_used=model_used
            )
            self.db.add(conversation)

            # Update task status
            task.status = "completed"
            task.result = response
            self.db.commit()

            return response

        except Exception as e:
            logger.error(f"Error processing input: {str(e)}")
            if task:
                task.status = "failed"
                task.result = str(e)
                self.db.commit()
            raise

    def _requires_deep_thinking(self, input_text: str) -> bool:
        """
        Determine if the input requires the O3-mini model for deep thinking.
        
        Args:
            input_text: The input text to analyze
        
        Returns:
            bool: True if O3-mini should be used, False for ChatGPT
        """
        # Add logic to determine which model to use
        # This is a simple implementation that can be enhanced
        deep_thinking_keywords = {'analyze', 'compare', 'evaluate', 'synthesize'}
        return any(keyword in input_text.lower() for keyword in deep_thinking_keywords)

    async def process_tasks(self) -> List[dict]:
        """
        Main task processing loop that:
        1. Loads tasks by urgency
        2. Chunks them appropriately
        3. Gets summaries and presents them
        4. Returns the list of tasks for further interaction
        
        Returns:
            List[dict]: List of all processed tasks
        """
        try:
            # Get all tasks ordered by urgency
            all_tasks = []
            for urgency in URGENCY_ORDER:
                tasks = get_tasks_by_urgency(urgency)
                all_tasks.extend(tasks)

                # Handle half-finished tasks with special priority
                if urgency == HALF_FINISHED_PRIORITY:
                    half_finished = [t for t in tasks if t['status'] == 'half-completed']
                    all_tasks.extend(half_finished)

            if not all_tasks:
                print("No tasks available.")
                return []

            # Chunk tasks for processing
            task_chunks = self._chunk_tasks(all_tasks)
            
            for i, chunk in enumerate(task_chunks, 1):
                print(f"\nTask Group {i}:")
                # Get summary from ChatGPT
                chunk_text = self._format_tasks_for_summary(chunk)
                summary = ""
                async for chunk in self.chatgpt.summarize_tasks(chunk_text):
                    summary += chunk
                print(summary)

            return all_tasks

        except Exception as e:
            logger.error(f"Error in task processing: {str(e)}")
            raise

    def _chunk_tasks(self, tasks: List[dict]) -> List[List[dict]]:
        """
        Split tasks into chunks based on MAX_TOKENS or MAX_EMAILS.
        
        Args:
            tasks: List of task dictionaries
        
        Returns:
            List of task chunks
        """
        chunks = []
        current_chunk = []
        current_size = 0

        for task in tasks:
            # Estimate token count (rough approximation)
            task_size = len(str(task)) // 4  # Rough estimate of tokens
            
            if (len(current_chunk) >= MAX_EMAILS or 
                current_size + task_size > MAX_TOKENS):
                if current_chunk:  # Don't add empty chunks
                    chunks.append(current_chunk)
                current_chunk = [task]
                current_size = task_size
            else:
                current_chunk.append(task)
                current_size += task_size

        if current_chunk:  # Add the last chunk if not empty
            chunks.append(current_chunk)

        return chunks

    def _format_tasks_for_summary(self, tasks: List[dict]) -> str:
        """
        Format a list of tasks into a string for summarization.
        
        Args:
            tasks: List of task dictionaries
        
        Returns:
            Formatted string of tasks
        """
        formatted_tasks = []
        for task in tasks:
            task_str = f"Task {task['id']}: {task['description']}\n"
            task_str += f"Urgency: {task['urgency']}\n"
            task_str += f"Status: {task['status']}\n"
            if task.get('alertAt'):
                task_str += f"Alert At: {task['alertAt']}\n"
            formatted_tasks.append(task_str)
        
        return "\n".join(formatted_tasks)

    async def process_selected_task(self, task_id: int) -> None:
        """
        Process a selected task by generating an action prompt and handling user interaction.
        
        Args:
            task_id: The ID of the task to process
            
        Raises:
            ValueError: If the task is not found
        """
        try:
            # Get the task from the database
            task = self.db.query(Task).filter(Task.id == task_id).first()
            if not task:
                raise ValueError(f"Task {task_id} not found")

            # Convert task to dictionary for the action prompt
            task_dict = {
                'id': task.id,
                'description': task.description,
                'urgency': task.urgency,
                'deadline': task.deadline.isoformat() if task.deadline else None,
                'category': task.category,
                'status': task.status
            }

            # Generate and display action prompt
            prompt = ""
            async for chunk in self.chatgpt.generate_action_prompt(task_dict):
                prompt += chunk
            print("\n" + prompt)

            while True:
                action = input("\nYour action (complete/remind/help/skip/back): ").strip().lower()
                
                if action == 'back':
                    break
                elif action == 'complete':
                    update_task_status(task_id, 'completed', None)
                    print(f"Task {task_id} marked as completed.")
                    break
                elif action == 'remind':
                    # Set reminder for tomorrow
                    reminder_time = datetime.utcnow() + timedelta(days=1)
                    update_task_status(task_id, task.status, reminder_time)
                    print(f"Reminder set for task {task_id} for tomorrow.")
                    break
                elif action == 'help':
                    update_task_status(task_id, 'half-completed', datetime.utcnow())
                    print(f"Task {task_id} marked as needing help. I'll help you break it down.")
                    
                    # Generate help response
                    help_response = ""
                    async for chunk in self.chatgpt.process(
                        f"Help me break down this task: {task.description}",
                        context={'task_id': task_id}
                    ):
                        help_response += chunk
                    print("\n" + help_response)
                    break
                elif action == 'skip':
                    print(f"Skipping task {task_id}.")
                    break
                else:
                    print("Invalid action. Available actions: complete, remind, help, skip, back")

        except Exception as e:
            logger.error(f"Error processing selected task: {str(e)}")
            raise

    def __del__(self):
        """Cleanup resources."""
        if hasattr(self, 'db') and self.db is not None:
            try:
                self.db.close()
            except Exception as e:
                logger.error(f"Error closing database connection: {str(e)}") 