from langchain_community.vectorstores import FAISS
from langchain_chroma import Chroma
from .load_model import embedding_model


def load_faiss():

    # Load the Faiss vector store
    faiss_vector_store = FAISS.load_local(
        "./src/common/faiss-vme", embedding_model, allow_dangerous_deserialization=True
    )

    # Query the vector stores
    results = faiss_vector_store.similarity_search(
        "LangChain provides abstractions to make working with LLMs easy",
        k=2,
        filter={"source": "tweet"},
    )
    for res in results:
        print(f"* {res.page_content} [{res.metadata}]")

    return results


def load_chroma():

    chroma_vector_store = Chroma(
        collection_name="vme-demo",
        embedding_function=embedding_model,
        persist_directory="./src/common/chroma-vme",  # Where to save data locally, remove if not necessary
    )

    # Query the vector stores
    results = chroma_vector_store.similarity_search(
        "LangChain provides abstractions to make working with LLMs easy",
        k=2,
        filter={"source": "tweet"},
    )
    for res in results:
        print(f"* {res.page_content} [{res.metadata}]")

    return results

