from flask import Flask, render_template, request, jsonify, redirect, url_for
import json
import os
from datetime import datetime
from pdfrw import PdfReader, PdfWriter
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)
FORM_STRUCTURE = "form_structure.json"
PDF_TEMPLATE = "form_template.pdf"
SERVICE_ACCOUNT_FILE = "gdrive_credentials.json"
GDRIVE_FOLDER_ID = "1-k6ibIf3L2nR5QB9DTw0TTzZw2SiCquM"  # Replace with your real folder ID

# Load Google Drive credentials
SCOPES = ['https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=SCOPES
)
drive_service = build('drive', 'v3', credentials=creds)

def fill_pdf_with_answers(input_pdf, output_pdf, answers):
    import io
    import os
    from pdfrw import PdfReader, PdfWriter, PdfDict, PdfName
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    from pypdf import PdfReader as PypdfReader
    from pypdf import PdfWriter as PypdfWriter

    # First: fill form using pdfrw
    pdf = PdfReader(input_pdf)
    checkmarks_by_page = {}

    for page_num, page in enumerate(pdf.pages):
        annotations = page.Annots
        if not annotations:
            continue

        for annotation in annotations:
            if annotation.Subtype == PdfName.Widget and annotation.T:
                field_name = str(annotation.T).strip("()").strip().lower()
                field_name = field_name.replace("checkbox", "check box")  # Normalize variations
                field_type = annotation.FT

                for answer in answers:
                    expected_field = answer["field_id"].strip().lower()
                    expected_field = expected_field.replace("checkbox", "check box")

                    if field_type == PdfName.Tx and expected_field.isdigit():
                        expected_field = f"text field {expected_field}"

                    if expected_field == field_name:
                        # print(f"🧩 Matched field: {field_name} ←→ {expected_field} (answer: {answer['answer']})")

                        if field_type == PdfName.Tx:
                            annotation.V = answer["answer"]
                            annotation.AP = None
                        elif field_type == PdfName.Btn:
                            if answer["answer"].strip().lower() in ["yes", "on", "true", "1"]:
                                annotation.V = PdfName.Yes
                                annotation.AS = PdfName.Yes

                                # save rect to draw ✓
                                rect = annotation.Rect
                                if rect:
                                    x = float(rect[0]) + 2
                                    y = float(rect[1]) + 2
                                    size = float(rect[3]) - float(rect[1]) - 4
                                   # print(f"🖍️ Drawing ✓ at page {page_num}, x={x}, y={y}, size={size}")
                                    if page_num not in checkmarks_by_page:
                                        checkmarks_by_page[page_num] = []
                                    checkmarks_by_page[page_num].append((x, y, size))
                            else:
                                annotation.V = PdfName.Off
                                annotation.AS = PdfName.Off
                        break

    # Save intermediate PDF (pdfrw)
    temp_path = output_pdf.replace(".pdf", "_temp.pdf")
    PdfWriter().write(temp_path, pdf)

    # Overlay checkmarks using pypdf + reportlab
    reader = PypdfReader(temp_path)
    writer = PypdfWriter()

    for page_num, page in enumerate(reader.pages):
        checkmarks = checkmarks_by_page.get(page_num, [])
        if checkmarks:
            packet = io.BytesIO()
            can = canvas.Canvas(packet, pagesize=letter)
            for x, y, size in checkmarks:
                can.setFont("Helvetica", size)
                can.drawString(x, y, "✓")
            can.save()
            packet.seek(0)
            overlay = PypdfReader(packet)
            page.merge_page(overlay.pages[0])
        writer.add_page(page)

    with open(output_pdf, "wb") as f:
        writer.write(f)

    os.remove(temp_path)


def upload_to_drive(filepath, filename, mimetype):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return None, None
    file_metadata = {
        'name': filename,
        'parents': [GDRIVE_FOLDER_ID]
    }
    media = MediaFileUpload(filepath, mimetype=mimetype)
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields='id, webViewLink'
    ).execute()
    print(f"Uploaded {filename} to Google Drive: {file['webViewLink']}")
    return file['id'], file['webViewLink']

@app.route("/", methods=["GET", "POST"])
def index():
    with open(FORM_STRUCTURE, "r", encoding="utf-8") as f:
        form_structure = json.load(f)["form_sections"]

    if request.method == "POST":
        output = []
        raw_answers = {}
        for item in form_structure:
            question = item["question"]
            if item["type"] == "text":
                val = request.form.get(question, "")
                raw_answers[question] = val
                output.append({"question": question, "field_id": item["field_name"], "answer": val})
            elif item["type"] == "checkbox":
                selected = request.form.get(question, "")
                raw_answers[question] = selected
                for option in item["options"]:
                    output.append({
                        "question": f"{question} - {option['label']}",
                        "field_id": option["field_id"],
                        "answer": "Yes" if selected == option["label"] else "No"
                    })
            elif item["type"] == "hybrid":
                raw_answers[question] = {}

                for opt in item["options"]:
                    input_name = f"{question}_{opt['label']}"
                    val = request.form.get(input_name)

                    # If it's a checkbox field and was not submitted at all, it's unchecked
                    if val is None:
                        # Assume "No" for missing checkbox
                        normalized_val = "No"
                    elif val.lower() in ["on", "yes", "true", "1"]:
                        normalized_val = "Yes"
                    else:
                        normalized_val = val  # this is probably text input like a date

                    raw_answers[question][opt["label"]] = normalized_val

                    output.append({
                        "question": f"{question} - {opt['label']}",
                        "field_id": opt["field_id"],
                        "answer": normalized_val
                    })

            elif item["type"] in ["table", "hybrid table"]:
                row_count = int(request.form.get(f"{question}_rows", 1))
                raw_answers[question] = []
                for i in range(row_count):
                    row_data = {}
                    for col in item["columns"]:
                        field_ids = col["field_ids"]
                        if i < len(field_ids):
                            val = request.form.get(f"{question}_{col['label']}_{i}", "")
                            row_data[col["label"]] = val
                            output.append({
                                "question": f"{question} - Row {i+1} - {col['label']}",
                                "field_id": field_ids[i],
                                "answer": val
                            })
                    raw_answers[question].append(row_data)

        name = request.form.get("Given name(s):", "noname").strip().replace(" ", "_")
        surname = request.form.get("Surname:", "nosurname").strip().replace(" ", "_")
        today = datetime.today().strftime("%Y-%m-%d")
        filename = f"{name}_{surname}_answers_{today}.json"
        pdf_filename = filename.replace(".json", ".pdf")

        # Save JSON to temp
        json_temp_path = f"/tmp/{filename}"
        with open(json_temp_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        # Generate PDF and save to temp
        pdf_temp_path = f"/tmp/{pdf_filename}"
        fill_pdf_with_answers(PDF_TEMPLATE, pdf_temp_path, output)

        print("PDF generated at:", pdf_temp_path, "Exists?", os.path.exists(pdf_temp_path))

        # Upload both to Google Drive
        json_id, json_link = upload_to_drive(json_temp_path, filename, "application/json")
        pdf_id, pdf_link = upload_to_drive(pdf_temp_path, pdf_filename, "application/pdf")

        return f"""
        <html>
        <head>
            <meta charset='utf-8'>
            <title>Submission Complete</title>
            <style>
                body {{ font-family: Arial, sans-serif; padding: 40px; background: #f4f8fc; }}
                .card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 8px rgba(0,0,0,0.1); max-width: 600px; margin: auto; text-align: center; }}
                a.button {{ background: #007bff; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px; }}
                a.button:hover {{ background: #0056b3; }}
            </style>
        </head>
        <body>
            <div class='card'>
                <h2>Submission successful!</h2>
                <p>Your PDF has been generated and uploaded.</p>
                <p><a class='button' href='{pdf_link}' target='_blank'>View PDF on Google Drive</a></p>
            </div>
        </body>
        </html>
        """

    return render_template("form.html", form_data=form_structure)

if __name__ == "__main__":
    app.run(host='0.0.0.0', debug=True, port=5050)

