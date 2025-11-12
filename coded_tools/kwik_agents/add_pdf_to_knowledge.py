"""Tool module for adding PDF documents to the knowledge base"""

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

import json
import logging
import os
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List

from langchain_community.document_loaders import PyMuPDFLoader
from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore
from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.base_rag import BaseRag

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DOCUMENT_REGISTRY_PATH = "./DocumentRegistry.json"
VECTOR_STORE_PATH = "./pdf_knowledge_vectorstore.json"


class AddPdfToKnowledge(CodedTool, BaseRag):
    """
    CodedTool implementation which adds a PDF document to the persistent knowledge base.

    This tool:
    1. Loads an existing vector store or creates a new one
    2. Processes the PDF and adds its chunks to the vector store
    3. Saves the updated vector store to disk
    4. Updates the document registry with metadata
    """

    def __init__(self):
        BaseRag.__init__(self)
        self.save_vector_store = True
        self.configure_vector_store_path(VECTOR_STORE_PATH)

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> str:
        """
        Add a PDF document to the knowledge base.

        :param args: Dictionary containing:
            "file_path": path to the uploaded PDF file

        :param sly_data: A dictionary whose keys are defined by the agent
            hierarchy, but whose values are meant to be kept out of the
            chat stream.

        :return: Success message with document metadata or error message
        """
        # Extract arguments
        file_path: str = args.get("file_path", "")

        # Validate presence of required inputs
        if not file_path:
            return "Error: Missing required input 'file_path'."

        # Validate file exists
        if not os.path.exists(file_path):
            return f"Error: File not found: {file_path}"

        # Validate file is a PDF
        if not file_path.lower().endswith('.pdf'):
            return f"Error: File must be a PDF: {file_path}"

        logger.info("Adding PDF to knowledge base: %s", file_path)

        try:
            # Get document metadata before processing
            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)

            # Load or create vector store
            vector_store: VectorStore = await self.generate_vector_store(
                loader_args={"urls": [file_path]},
                vector_store_type="in_memory"
            )

            if not vector_store:
                return "Error: Failed to create or load vector store."

            # Load the PDF to get page count
            loader = PyMuPDFLoader(file_path=file_path)
            docs: List[Document] = await loader.aload()
            page_count = len(docs)

            # Update document registry
            registry_entry = {
                "filename": filename,
                "file_path": file_path,
                "upload_date": datetime.now().isoformat(),
                "page_count": page_count,
                "file_size_bytes": file_size,
                "status": "processed"
            }

            self._update_document_registry(registry_entry)

            logger.info("Successfully added PDF to knowledge base: %s", filename)

            return f"Successfully added {filename} to knowledge base - {page_count} pages processed."

        except FileNotFoundError:
            logger.error("File not found: %s", file_path)
            return f"Error: File not found: {file_path}"

        except ValueError as e:
            logger.error("Invalid file or unsupported input: %s - %s", file_path, e)
            return f"Error: Invalid file or unsupported input: {file_path} - {e}"

        except Exception as e:
            logger.error("Error adding PDF to knowledge base: %s", e)
            return f"Error: Failed to add PDF to knowledge base: {e}"

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> str:
        """
        Synchronous invoke - not supported, use async_invoke.
        """
        return "Error: This tool requires async execution. Please use async_invoke."

    async def load_documents(self, loader_args: Dict[str, Any]) -> List[Document]:
        """
        Load PDF documents from file paths.

        :param loader_args: Dictionary containing 'urls' (list of PDF file paths)
        :return: List of loaded PDF documents
        """
        docs: List[Document] = []
        urls: List[str] = loader_args.get("urls", [])

        for file_path in urls:
            try:
                loader = PyMuPDFLoader(file_path=file_path)
                doc: List[Document] = await loader.aload()
                docs.extend(doc)
                logger.info("Successfully loaded PDF file from %s", file_path)
            except FileNotFoundError:
                logger.error("File not found: %s", file_path)
            except ValueError as e:
                logger.error("Invalid file path or unsupported input: %s – %s", file_path, e)

        return docs

    def _update_document_registry(self, entry: Dict[str, Any]) -> None:
        """
        Update the document registry with new entry.

        :param entry: Document metadata to add to registry
        """
        try:
            # Load existing registry
            if os.path.exists(DOCUMENT_REGISTRY_PATH):
                with open(DOCUMENT_REGISTRY_PATH, "r", encoding="utf-8") as f:
                    registry = json.load(f)
            else:
                registry = {"documents": []}

            # Add new entry
            registry["documents"].append(entry)

            # Save updated registry
            with open(DOCUMENT_REGISTRY_PATH, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2)

            logger.info("Updated document registry: %s", entry["filename"])

        except Exception as e:
            logger.error("Failed to update document registry: %s", e)
