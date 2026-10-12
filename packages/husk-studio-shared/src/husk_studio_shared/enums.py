from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    ABORTED = "aborted"


class SpanStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


class SpanKind(StrEnum):
    LLM = "llm"
    TOOL = "tool"
    CHAIN = "chain"
    AGENT_DECISION = "agent_decision"
    GRAPH_NODE = "graph_node"
    STATE_SET = "state_set"


class OverrideType(StrEnum):
    INPUT_REPLACE = "input_replace"
    PROMPT_EDIT = "prompt_edit"
    MODEL_SWAP = "model_swap"


class Framework(StrEnum):
    LANGCHAIN = "langchain"
    LANGGRAPH = "langgraph"
    MIXED = "mixed"
    UNKNOWN = "unknown"
