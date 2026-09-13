from __future__ import annotations

import tempfile
from pathlib import Path

from flask import Flask, request
from werkzeug.utils import secure_filename

from src.gpay_analytics import analyze, dashboard_html, extract_pdf_text, load_config, parse_transactions, upload_page_html


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

def analyze_upload(upload) -> str:
    if upload is None or not upload.filename:
        raise ValueError("Choose a PDF statement to upload")
    filename = secure_filename(upload.filename or "statement.pdf")
    if not filename.lower().endswith(".pdf"):
        raise ValueError("Please upload a PDF statement")

    with tempfile.TemporaryDirectory() as directory:
        pdf_path = Path(directory) / filename
        upload.save(pdf_path)
        categories, credit_keywords = load_config(Path(__file__).with_name("config.toml"))
        transactions = parse_transactions(extract_pdf_text(pdf_path), categories, credit_keywords)
        if not transactions:
            raise ValueError("No transactions detected. Make sure the PDF has selectable text.")
        payload = {
            "source": filename,
            "transactions": [transaction.__dict__ for transaction in transactions],
            "analysis": analyze(transactions),
        }
        return dashboard_html(pdf_path, payload).replace(
            "</body>",
            '<p style="position:fixed;left:18px;bottom:18px;margin:0"><a href="/" style="padding:10px 14px;background:#0f6b52;color:white;text-decoration:none;border-radius:5px;font:700 12px ui-monospace,monospace">Analyze another</a></p></body>',
        )


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "POST":
        try:
            return analyze_upload(request.files.get("statement"))
        except (RuntimeError, ValueError) as error:
            return upload_page_html(str(error)), 400
    return upload_page_html()


if __name__ == "__main__":
    app.run(debug=True, port=3000)