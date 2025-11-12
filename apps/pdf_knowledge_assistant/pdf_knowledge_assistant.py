import os

from neuro_san.client.agent_session_factory import AgentSessionFactory
from neuro_san.client.streaming_input_processor import StreamingInputProcessor

AGENT_NETWORK_NAME = "pdf_knowledge_agent"


def set_up_pdf_knowledge_assistant():
    """Configure and initialize the PDF knowledge assistant."""
    agent_name = AGENT_NETWORK_NAME
    connection = "direct"
    host = "localhost"
    port = 30011
    local_externals_direct = False
    metadata = {"user_id": os.environ.get("USER", "default_user")}

    # Create session factory and agent session
    factory = AgentSessionFactory()
    session = factory.create_session(connection, agent_name, host, port, local_externals_direct, metadata)

    # Initialize conversation state
    knowledge_thread = {
        "last_chat_response": None,
        "prompt": "Please enter your query or upload a PDF:\n",
        "timeout": 5000.0,
        "num_input": 0,
        "user_input": None,
        "sly_data": None,
        "chat_filter": {"chat_filter_type": "MAXIMAL"},
    }

    return session, knowledge_thread


def process_user_query(knowledge_session, knowledge_thread, user_input):
    """
    Process a user query within the PDF knowledge assistant's session.

    This function handles a single interaction by:
    1. Initializing a StreamingInputProcessor to handle the input
    2. Updating the agent's internal thread state with the user's input
    3. Passing the updated thread to the processor for handling
    4. Extracting and returning the agent's response

    Parameters:
        knowledge_session: An active session object for the knowledge assistant
        knowledge_thread (dict): The agent's current conversation thread state
        user_input (str): The user's query to be processed

    Returns:
        tuple:
            - last_chat_response (str or None): The agent's response to the input
            - knowledge_thread (dict): The updated thread state after processing
    """
    # Use the processor to handle the input
    input_processor = StreamingInputProcessor(
        "DEFAULT",
        "/tmp/agent_thinking.txt",
        knowledge_session,
        None,  # Not using a thinking_dir
    )

    # Update the conversation state with this turn's input
    knowledge_thread["user_input"] = user_input
    knowledge_thread = input_processor.process_once(knowledge_thread)

    # Get the agent response for this turn
    last_chat_response = knowledge_thread.get("last_chat_response")

    return last_chat_response, knowledge_thread


def process_pdf_upload(knowledge_session, knowledge_thread, file_path):
    """
    Process a PDF upload by instructing the agent to add it to the knowledge base.

    Parameters:
        knowledge_session: An active session object for the knowledge assistant
        knowledge_thread (dict): The agent's current conversation thread state
        file_path (str): Path to the uploaded PDF file

    Returns:
        tuple:
            - last_chat_response (str or None): The agent's response after processing
            - knowledge_thread (dict): The updated thread state after processing
    """
    upload_instruction = (
        f"A PDF file has been uploaded at: {file_path}\n"
        f"Please add this document to the knowledge base and extract key topics and facts from it."
    )

    return process_user_query(knowledge_session, knowledge_thread, upload_instruction)


def tear_down_pdf_knowledge_assistant(knowledge_session):
    """
    Tear down the assistant and close the session.

    :param knowledge_session: The pointer to the session.
    """
    print("Tearing down PDF knowledge assistant...")
    knowledge_session.close()
    print("PDF knowledge assistant torn down.")
