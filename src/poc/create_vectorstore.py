import chromadb
import faiss
import weaviate
from langchain_weaviate.vectorstores import WeaviateVectorStore
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from langchain_chroma import Chroma
from .load_model import embedding_model
from .documents import documents, uuids

def create_faiss():

    # Create FAISS vector store
    index = faiss.IndexFlatL2(len(embedding_model.embed_query("hello world")))
    faiss_vector_store = FAISS(
        embedding_function=embedding_model,
        index=index,
        docstore=InMemoryDocstore(),
        index_to_docstore_id={},
    )

    faiss_vector_store.add_documents(documents=documents, ids=uuids)

    faiss_vector_store.save_local("./src/common/faiss-vme")

def create_chroma():

    # Create ChromaDB vector store
    chroma_persistent_client = chromadb.PersistentClient("./src/common/chroma-vme")
    chroma_vector_store_from_client = Chroma(
        client=chroma_persistent_client,
        collection_name="vme-demo",
        embedding_function=embedding_model,
    )

    chroma_vector_store_from_client.add_documents(documents=documents, ids=uuids)

def create_weaviate():

    # Create Weaviate vector store
    weaviate_client = weaviate.connect_to_local()
    weaviate_vector_store = WeaviateVectorStore.from_documents(documents=documents, ids=uuids, embedding=embedding_model, client=weaviate_client, index_name = "kgedemo", text_key= "kge")
    results = weaviate_vector_store.similarity_search(
        "LangChain provides abstractions to make working with LLMs easy"
    )
    for i, doc in enumerate(results):
        print(f"\nDocument {i+1}:")
        print(doc.page_content[:100] + "...")
    weaviate_client.close()

    
    # A --> B (requirement)