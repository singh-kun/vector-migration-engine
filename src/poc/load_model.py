from langchain_ollama import ChatOllama, OllamaEmbeddings

# Initialize models
embedding_model = OllamaEmbeddings(
    model="llama3.2",
)
generation_model = ChatOllama(
    model="llama3.2",
)
