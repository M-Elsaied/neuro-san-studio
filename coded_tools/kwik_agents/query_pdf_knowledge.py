"""Tool module for querying the PDF knowledge base"""

# Copyright (C) 2023-2025 Cognizant Digital Business, Evolutionary AI.
# All Rights Reserved.
# Issued under the Academic Public License.
#
# You can be released from the terms, and requirements of the Academic Public
# License by purchasing a commercial license.
# Purchase of a commercial license is mandatory for any use of the
# neuro-san SDK Software in commercial settings.
#
# END COPYRIGHT

import logging
import os
from typing import Any
from typing import Dict
from typing import List

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.base_rag import BaseRag

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VECTOR_STORE_PATH = "./pdf_knowledge_vectorstore.json"


class QueryPdfKnowledge(CodedTool, BaseRag):
    """
    CodedTool implementation for querying the PDF knowledge base.

    This tool:
    1. Loads the existing vector store from disk
    2. Performs similarity search based on the query
    3. Returns relevant document chunks
    """

    def __init__(self):
        BaseRag.__init__(self)
        self.configure_vector_store_path(VECTOR_STORE_PATH)

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> str:
        """
        Query the PDF knowledge base using vector similarity search.

        :param args: Dictionary containing:
            "query": search string to find relevant document chunks

        :param sly_data: A dictionary whose keys are defined by the agent
            hierarchy, but whose values are meant to be kept out of the
            chat stream.

        :return: Relevant document chunks or error message
        """
        # Extract arguments
        query: str = args.get("query", "")

        # Validate presence of required inputs
        if not query:
            return "Error: Missing required input 'query'."

        logger.info("Querying PDF knowledge base: %s", query)

        # Check if vector store exists
        if not os.path.exists(VECTOR_STORE_PATH):
            return "Error: No knowledge base found. Please upload PDF documents first."

        try:
            # Load existing vector store
            vector_store: VectorStore = await self._load_existing_vector_store()

            if not vector_store:
                return "Error: Failed to load knowledge base. The vector store may be corrupted."

            # Query the vector store
            result = await self.query_vectorstore(vector_store, query)

            if not result or result.strip() == "":
                return "No relevant information found in the knowledge base for this query."

            logger.info("Successfully queried PDF knowledge base")

            return result

        except FileNotFoundError:
            logger.error("Vector store file not found: %s", VECTOR_STORE_PATH)
            return "Error: Knowledge base file not found. Please upload PDF documents first."

        except Exception as e:
            logger.error("Error querying PDF knowledge base: %s", e)
            return f"Error: Failed to query knowledge base: {e}"

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> str:
        """
        Synchronous invoke - not supported, use async_invoke.
        """
        return "Error: This tool requires async execution. Please use async_invoke."

    async def load_documents(self, loader_args: Dict[str, Any]) -> List[Document]:
        """
        Not used for querying - this tool only loads existing vector store.

        :param loader_args: Not used
        :return: Empty list
        """
        return []
