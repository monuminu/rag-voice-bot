import json
import random
import chainlit as cl
from datetime import datetime, timedelta
import uuid
from azure.core.credentials import AzureKeyCredential
from azure.identity import get_bearer_token_provider
from azure.search.documents import SearchClient
from openai import AzureOpenAI
import os

search_client = SearchClient(
    endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
    index_name=os.environ["INDEX_NAME"],
    credential=AzureKeyCredential(os.environ["AZURE_SEARCH_KEY"]) 
)

fetch_relevant_documents_def = {
    "name": "fetch_relevant_documents",
    "description": "Fetch relevant documents for a query",
    "parameters": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "query for which documents are to be fetched"
        }
      },
      "required": ["query"]
    }
}
  
async def fetch_relevant_documents_handler(params):
    query = params['query']
    search_results = search_client.search(
        search_text=query,
        top=5,
        select="content"
    )
    sources_formatted = "\n".join([f'{document["content"]}' for document in search_results])
    return sources_formatted

# Tools list
tools = [
    (fetch_relevant_documents_def, fetch_relevant_documents_handler),
]