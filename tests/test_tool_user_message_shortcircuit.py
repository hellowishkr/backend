"""Tool errors must reach the model so it can explain them in the user's language."""


from app.bot import agent
from app.bot.tools import adapter


async def test_tool_english_error_does_not_override_model_localized_reply(db, model_turn, monkeypatch):
    async def failed_tool(*args):
        return {"error": "lookup_failed", "user_message": "Could not find that order."}
    monkeypatch.setitem(adapter.FUNCTION_MAP, "get_order_support", failed_tool)
    seen = model_turn("No pude verificar ese pedido.", [("get_order_support", {})])
    reply = await agent.handle_message(db, "919000000001", "¿Dónde está mi pedido?")
    assert seen["results"][0]["error"] == "lookup_failed"
    assert reply == "No pude verificar ese pedido."


def test_runner_keeps_tool_results_in_the_model_loop():
    runner = agent._build_runner("unregistered", "gemini-2.5-flash", [])
    assert runner.agent.after_tool_callback is None
