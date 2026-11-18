import gradio as gr
from dotenv import load_dotenv
from dewey import Dewey
from models import NewsroomConfig
from providers import build_stack
import uuid

# interpolate=False: values are literal, so a password containing "${" is not expanded.
load_dotenv(interpolate=False)

# DEWEY_STACK=azure (default) or anthropic; see providers/__init__.py
llm, retriever = build_stack(NewsroomConfig.from_env())
dewey = Dewey(llm, retriever)


def chat_with_dewey(message, history, session_state):
    # Initialize session if needed
    if session_state["session_id"] is None:
        session_state["session_id"] = str(uuid.uuid4())
        session_state["user_data"]["message_count"] = 0
    
    # Track message count
    session_state["user_data"]["message_count"] += 1
    
    # Add user message to history and show it immediately
    history.append({"role": "user", "content": message})
    yield history, session_state

    
    # Stream the response from Dewey
    history_length = len(history)
    for response, steps in dewey.process(message, history[:-1]):
        history = history[:history_length]
        for step in steps:
            history.append({
                "role": "assistant",
                "content": f"{step.get('content', '')}",
                "metadata": {
                    "title": step["title"],
                    "status": step["status"]
                }
            })
        history.append({
            "role": "assistant",
            "content": response
        })
        
        yield history, session_state

with gr.Blocks(title="Dewey") as demo:
    gr.Markdown("# Dewey")
    gr.Markdown("A newsroom archive assistant.")
    
    # Session state
    session_state = gr.State({"session_id": None, "user_data": {}})
    
    # Gradio 6 always uses the messages format (no `type`). `buttons=[]` and
    # `allow_tags=False` keep Gradio 5's defaults: no share/copy buttons, and no
    # custom HTML tags kept in rendered messages.
    chatbot = gr.Chatbot(label="Dewey", buttons=[], allow_tags=False)
    msg = gr.Textbox(label="Message", placeholder="Ask your question here...")
    clear = gr.Button("Clear")
    
    msg.submit(chat_with_dewey, [msg, chatbot, session_state], [chatbot, session_state])
    msg.submit(lambda: "", None, [msg])
    clear.click(lambda: [], None, [chatbot])

demo.launch()
