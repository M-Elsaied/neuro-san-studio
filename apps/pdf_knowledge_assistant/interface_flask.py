import atexit
import json
import os
from datetime import datetime
from werkzeug.utils import secure_filename

from flask import Flask
from flask import jsonify
from flask import render_template
from flask import request
from flask_socketio import SocketIO

from pdf_knowledge_assistant import process_pdf_upload
from pdf_knowledge_assistant import process_user_query
from pdf_knowledge_assistant import set_up_pdf_knowledge_assistant
from pdf_knowledge_assistant import tear_down_pdf_knowledge_assistant

os.environ["AGENT_MANIFEST_FILE"] = "registries/manifest.hocon"
os.environ["AGENT_TOOL_PATH"] = "coded_tools"

app = Flask(__name__)
app.config["SECRET_KEY"] = "pdf_knowledge_secret_key"
app.config["UPLOAD_FOLDER"] = "apps/pdf_knowledge_assistant/static/uploads"
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB max file size

ALLOWED_EXTENSIONS = {"pdf"}

socketio = SocketIO(app)

knowledge_session, knowledge_thread = set_up_pdf_knowledge_assistant()

DOCUMENT_REGISTRY_PATH = "./DocumentRegistry.json"
TOPIC_MEMORY_PATH = "./TopicMemory.json"


def allowed_file(filename):
    """Check if the uploaded file has an allowed extension."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/")
def index():
    """Return the main HTML page."""
    return render_template("index.html", year=datetime.now().year)


@app.route("/upload", methods=["POST"])
def upload_file():
    """Handle PDF file upload."""
    global knowledge_thread

    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    file = request.files["file"]

    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Only PDF files are allowed"}), 400

    try:
        # Secure the filename and save
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config["UPLOAD_FOLDER"], unique_filename)

        # Ensure upload directory exists
        os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

        # Save the file
        file.save(filepath)

        # Convert to absolute path for the agent
        abs_filepath = os.path.abspath(filepath)

        # Process the PDF upload through the agent
        response, knowledge_thread = process_pdf_upload(knowledge_session, knowledge_thread, abs_filepath)

        return jsonify({
            "success": True,
            "message": response,
            "filename": unique_filename,
            "filepath": abs_filepath
        })

    except Exception as e:
        return jsonify({"error": f"Failed to process file: {str(e)}"}), 500


@socketio.on("user_query", namespace="/chat")
def handle_user_query(json_data):
    """Handle user queries through WebSocket."""
    global knowledge_thread

    user_query = json_data.get("data", "")

    if not user_query:
        socketio.emit("agent_response", {"data": "Error: Empty query received."}, namespace="/chat")
        return

    # Echo user query
    socketio.emit("user_message", {"data": user_query}, namespace="/chat")

    try:
        # Process the query through the agent
        response, knowledge_thread = process_user_query(knowledge_session, knowledge_thread, user_query)

        # Send agent response
        socketio.emit("agent_response", {"data": response or "No response from agent."}, namespace="/chat")

    except Exception as e:
        socketio.emit("agent_response", {"data": f"Error processing query: {str(e)}"}, namespace="/chat")


@app.route("/topics", methods=["GET"])
def get_topics():
    """Get the list of all topics in memory."""
    try:
        if os.path.exists(TOPIC_MEMORY_PATH):
            with open(TOPIC_MEMORY_PATH, "r", encoding="utf-8") as f:
                memory = json.load(f)
                topics = list(memory.keys())
                return jsonify({"topics": topics})
        else:
            return jsonify({"topics": []})
    except Exception as e:
        return jsonify({"error": f"Failed to load topics: {str(e)}"}), 500


@app.route("/topics/<topic>", methods=["GET"])
def get_topic_facts(topic):
    """Get all facts for a specific topic."""
    try:
        if os.path.exists(TOPIC_MEMORY_PATH):
            with open(TOPIC_MEMORY_PATH, "r", encoding="utf-8") as f:
                memory = json.load(f)
                facts = memory.get(topic, "No facts found for this topic.")
                return jsonify({"topic": topic, "facts": facts})
        else:
            return jsonify({"topic": topic, "facts": "No memory found."})
    except Exception as e:
        return jsonify({"error": f"Failed to load topic facts: {str(e)}"}), 500


@app.route("/documents", methods=["GET"])
def get_documents():
    """Get the list of uploaded documents."""
    try:
        if os.path.exists(DOCUMENT_REGISTRY_PATH):
            with open(DOCUMENT_REGISTRY_PATH, "r", encoding="utf-8") as f:
                registry = json.load(f)
                return jsonify(registry)
        else:
            return jsonify({"documents": []})
    except Exception as e:
        return jsonify({"error": f"Failed to load documents: {str(e)}"}), 500


@app.route("/stats", methods=["GET"])
def get_stats():
    """Get statistics about the knowledge base."""
    try:
        doc_count = 0
        topic_count = 0

        if os.path.exists(DOCUMENT_REGISTRY_PATH):
            with open(DOCUMENT_REGISTRY_PATH, "r", encoding="utf-8") as f:
                registry = json.load(f)
                doc_count = len(registry.get("documents", []))

        if os.path.exists(TOPIC_MEMORY_PATH):
            with open(TOPIC_MEMORY_PATH, "r", encoding="utf-8") as f:
                memory = json.load(f)
                topic_count = len(memory.keys())

        return jsonify({
            "document_count": doc_count,
            "topic_count": topic_count
        })
    except Exception as e:
        return jsonify({"error": f"Failed to load stats: {str(e)}"}), 500


def cleanup():
    """Tear things down on exit."""
    print("Shutting down PDF Knowledge Assistant...")
    tear_down_pdf_knowledge_assistant(knowledge_session)
    socketio.stop()


@app.route("/shutdown")
def shutdown():
    """Shutdown the application."""
    cleanup()
    return "PDF Knowledge Assistant shut down successfully."


@app.after_request
def add_header(response):
    """Add cache control header."""
    response.headers["Cache-Control"] = "no-store"
    return response


# Register the cleanup function
atexit.register(cleanup)

if __name__ == "__main__":
    socketio.run(app, debug=False, port=5002, allow_unsafe_werkzeug=True, log_output=True, use_reloader=False)
