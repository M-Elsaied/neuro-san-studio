"""Tool module for extracting structured knowledge from PDF documents"""

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

VECTOR_STORE_PATH = "./pdf_knowledge_vectorstore.json"


class ExtractPdfKnowledge(CodedTool, BaseRag):
    """
    CodedTool implementation for extracting structured knowledge from PDFs.

    This tool:
    1. Loads the PDF document
    2. Uses RAG to query for summaries and key points
    3. Analyzes content to identify topics and facts
    4. Returns structured data for consumption by commit_to_memory
    """

    def __init__(self):
        BaseRag.__init__(self)
        self.configure_vector_store_path(VECTOR_STORE_PATH)

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> str:
        """
        Extract structured knowledge from a PDF document.

        :param args: Dictionary containing:
            "file_path": path to the PDF file
            "focus_areas": (optional) list of specific topics to focus on

        :param sly_data: A dictionary whose keys are defined by the agent
            hierarchy, but whose values are meant to be kept out of the
            chat stream.

        :return: Structured summary of topics and facts, or error message
        """
        # Extract arguments
        file_path: str = args.get("file_path", "")
        focus_areas: List[str] = args.get("focus_areas", [])

        # Validate presence of required inputs
        if not file_path:
            return "Error: Missing required input 'file_path'."

        # Validate file exists
        if not os.path.exists(file_path):
            return f"Error: File not found: {file_path}"

        # Validate file is a PDF
        if not file_path.lower().endswith('.pdf'):
            return f"Error: File must be a PDF: {file_path}"

        logger.info("Extracting knowledge from PDF: %s", file_path)

        try:
            # Load the PDF document
            loader = PyMuPDFLoader(file_path=file_path)
            docs: List[Document] = await loader.aload()

            if not docs:
                return f"Error: Failed to load document or document is empty: {file_path}"

            filename = os.path.basename(file_path)
            page_count = len(docs)

            # Extract text content from all pages
            full_text = "\n\n".join([doc.page_content for doc in docs])

            # Limit text size to avoid token limits (take first ~10000 characters for summary)
            text_sample = full_text[:10000] if len(full_text) > 10000 else full_text

            # Generate structured summary
            summary = self._generate_document_summary(
                filename=filename,
                page_count=page_count,
                text_sample=text_sample,
                focus_areas=focus_areas
            )

            logger.info("Successfully extracted knowledge from PDF: %s", filename)

            return summary

        except FileNotFoundError:
            logger.error("File not found: %s", file_path)
            return f"Error: File not found: {file_path}"

        except ValueError as e:
            logger.error("Invalid file or unsupported input: %s - %s", file_path, e)
            return f"Error: Invalid file or unsupported input: {file_path} - {e}"

        except Exception as e:
            logger.error("Error extracting knowledge from PDF: %s", e)
            return f"Error: Failed to extract knowledge from PDF: {e}"

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

    def _generate_document_summary(
        self,
        filename: str,
        page_count: int,
        text_sample: str,
        focus_areas: List[str]
    ) -> str:
        """
        Generate a structured summary of the document for the agent to process.

        This returns a natural language summary that the agent can then use
        to extract topics and facts using commit_to_memory tool.

        :param filename: Name of the PDF file
        :param page_count: Number of pages in the document
        :param text_sample: Sample text from the document
        :param focus_areas: Specific topics to focus on (if any)
        :return: Structured summary text
        """
        # Build summary header
        summary_parts = [
            f"Document: {filename}",
            f"Pages: {page_count}",
            "",
            "CONTENT OVERVIEW:",
            ""
        ]

        # Add focus areas if provided
        if focus_areas:
            summary_parts.append(f"Focus areas requested: {', '.join(focus_areas)}")
            summary_parts.append("")

        # Add text sample introduction
        summary_parts.append("The following is a sample of the document content:")
        summary_parts.append("")
        summary_parts.append(text_sample)
        summary_parts.append("")
        summary_parts.append("---")
        summary_parts.append("")
        summary_parts.append(
            "Please analyze the above content and identify key topics and facts. "
            "For each significant topic you identify, use the commit_to_memory tool "
            "to store relevant facts under that topic."
        )

        if focus_areas:
            summary_parts.append(
                f"Pay special attention to information related to: {', '.join(focus_areas)}."
            )

        return "\n".join(summary_parts)
