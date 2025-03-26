from langchain_community.vectorstores import FAISS
from langchain_chroma import Chroma

class TransformFaissToChroma:
    
    """
    This function will transform the data from the Faiss vector store to the Chroma
    
    Steps:
    1. Load the Faiss vector store
    2. Get vectors, chunks and mappings -- faiss_vector_store.get_vectors(), faiss_vector_store.get_chunks(), faiss_vector_store.get_mappings()
    3. Use the above information to create a Chroma vector store
    
    """
    
    def __init__(self, faiss_vector_store, embedding_model):
        self.faiss_vector_store = faiss_vector_store
        self.embedding_model = embedding_model
    
    def transform(self):
        vectors = self.faiss_vector_store.get_vectors()
        chunks = self.faiss_vector_store.get_chunks()
        mappings = self.faiss_vector_store.get_mappings()
        
        chroma_vector_store = Chroma(
            collection_name="vme-demo",
            embedding_function=self.embedding_model,
            persist_directory="./src/common/chroma-vme",  # Where to save data locally, remove if not necessary
        )
        
        chroma_vector_store.insert(vectors, chunks, mappings)
        
        return chroma_vector_store
    
class TranformWeaviateToChroma:
    
    """
    This function will transform the data from the Weaviate to the Chroma
    
    Steps:
    1. Load the Weaviate vector store
    2. Get vectors, chunks and mappings -- weaviate_vector_store.get_vectors(), weaviate_vector_store.get_chunks(), weaviate_vector_store.get_mappings()
    3. Use the above information to create a Chroma vector store
    
    """
    
    def __init__(self, weaviate_vector_store, embedding_model):
        self.weaviate_vector_store = weaviate_vector_store
        self.embedding_model = embedding_model
    
    def transform(self):
        vectors = self.weaviate_vector_store.get_vectors()
        chunks = self.weaviate_vector_store.get_chunks()
        mappings = self.weaviate_vector_store.get_mappings()
        
        chroma_vector_store = Chroma(
            collection_name="vme-demo",
            embedding_function=self.embedding_model,
            persist_directory="./src/common/chroma-vme",  # Where to save data locally, remove if not necessary
        )
        
        chroma_vector_store.insert(vectors, chunks, mappings)
        
        return chroma_vector_store