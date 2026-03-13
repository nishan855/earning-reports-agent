import logging
from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph

from agent.state import AgentState
from agent.nodes import (
    intake_node,
    research_router_node,
    transcript_node,
    sec_node,
    news_node,
    competitor_node,
    synthesis_node,
    pattern_detection_node,
    report_writer_node,
)

logger = logging.getLogger(__name__)


def should_continue(state: AgentState) -> str:
    if state.data_sufficient or state.iteration_count >= state.max_iterations:
        logger.info(
            f"Proceeding to signal | sufficient: {state.data_sufficient} | iterations: {state.iteration_count}"
        )
        return "signal"
    logger.info(f"Looping back | iteration: {state.iteration_count}")
    return "loop"


def build_graph() -> CompiledStateGraph:
    graph = StateGraph(AgentState)

    graph.add_node("intake", intake_node)
    graph.add_node("research_router", research_router_node)
    graph.add_node("transcript", transcript_node)
    graph.add_node("sec", sec_node)
    graph.add_node("news", news_node)
    graph.add_node("competitor", competitor_node)
    graph.add_node("synthesis", synthesis_node)
    graph.add_node("pattern_detection", pattern_detection_node)
    graph.add_node("report_writer", report_writer_node)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "research_router")

    graph.add_edge("research_router", "transcript")
    graph.add_edge("research_router", "sec")
    graph.add_edge("research_router", "news")
    graph.add_edge("research_router", "competitor")

    graph.add_edge("transcript", "synthesis")
    graph.add_edge("sec", "synthesis")
    graph.add_edge("news", "synthesis")
    graph.add_edge("competitor", "synthesis")

    graph.add_edge("synthesis", "pattern_detection")

    graph.add_conditional_edges(
        "pattern_detection",
        should_continue,
        {
            "signal": "report_writer",
            "loop": "research_router",
        },
    )

    graph.add_edge("report_writer", END)

    return graph.compile()


agent = build_graph()
