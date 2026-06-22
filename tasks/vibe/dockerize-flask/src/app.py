"""A small existing Flask service. (No containerization yet.)"""

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify({"service": "widget-api", "status": "ok"})


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


@app.route("/widgets")
def widgets():
    return jsonify([
        {"id": 1, "name": "sprocket"},
        {"id": 2, "name": "cog"},
    ])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
