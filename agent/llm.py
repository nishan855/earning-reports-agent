from langchain_openai import ChatOpenAI

llm_mini = ChatOpenAI(
    model="gpt-5-mini",
    temperature=0,
)

llm = ChatOpenAI(
    model="gpt-5.4",
    temperature=0,
)
