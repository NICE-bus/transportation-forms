import streamlit as st
from streamlit_drawable_canvas import st_canvas
import pandas as pd
import datetime
import io
from PIL import Image, ImageOps
import gspread
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.utils import ImageReader, simpleSplit
import numpy as np
import msal
import requests
import base64

# --- Hide Streamlit Style ---
hide_streamlit_style = """
            <style>
            header {display: none;}
            #MainMenu {display: none;}
            footer {display: none;}
            </style>
            """
st.markdown(hide_streamlit_style, unsafe_allow_html=True) 

def highlight_missing_field(field_key, form_type):
    """Displays a 'required' message if the field is marked as missing in session_state."""
    session_key = f"missing_{form_type}_fields"
    if field_key in st.session_state.get(session_key, []):
        st.markdown(''':red-background[THIS FIELD IS REQUIRED] :arrow_down:''', unsafe_allow_html=True)

def display_submit_button_error(form_type, required_fields):
    """Displays a styled message above the submit button for a specific form, listing missing fields."""
    session_key = f"missing_{form_type}_fields"
    missing_keys = st.session_state.get(session_key, [])
    if missing_keys:
        missing_labels = [required_fields[key][0] for key in missing_keys if key in required_fields]
        st.markdown(
            f''':red-background[PLEASE FILL IN ALL REQUIRED FIELDS: {", ".join(missing_labels)}]''',
            unsafe_allow_html=True
        )

# Helper Functions
def send_pdf_email(pdf_file, subject, body, to_email, cc_emails=None):
    """Sends an email with a PDF attachment using Microsoft Graph API and OAuth."""
    st.info("Attempting to send email...")

    # 1. Get credentials from secrets
    # Streamlit converts all secret keys to lowercase. Use .get() to avoid errors if a key is missing.
    tenant_id = st.secrets.get("tenant_id")
    client_id = st.secrets.get("client_id")
    client_secret = st.secrets.get("client_secret")
    sender_email = st.secrets.get("email_user")

    if not to_email:
        error_msg = "Recipient email address (to_emails) is not set correctly in secrets."
        st.error(error_msg)
        return False, error_msg

    if not all([tenant_id, client_id, client_secret, sender_email]):
        error_msg = "Azure App credentials (tenant_id, client_id, client_secret) and sender email (email_user) are not set correctly in secrets."
        st.error(error_msg)
        return False, error_msg

    st.info("✅ Credentials loaded successfully.")

    # 2. Authenticate and get an access token
    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id, authority=authority, client_credential=client_secret
    )
    scopes = ["https://graph.microsoft.com/.default"]
    result = app.acquire_token_for_client(scopes=scopes)

    if "access_token" not in result:
        error_description = result.get("error_description", "No error description provided.")
        error_msg = f"Failed to acquire access token: {error_description}"
        st.error(error_msg)
        return False, error_msg

    access_token = result["access_token"]
    st.info("✅ Access token acquired.")

    # 3. Prepare the email payload for Graph API
    # Read and base64-encode the attachment
    with open(pdf_file, "rb") as f:
        attachment_content = base64.b64encode(f.read()).decode('utf-8')

    # Format recipients
    to_recipients = [{"emailAddress": {"address": email.strip()}} for email in to_email.split(',')]
    cc_recipients = []
    if cc_emails:
        if isinstance(cc_emails, str):
            cc_emails = [e.strip() for e in cc_emails.split(',')]
        cc_recipients = [{"emailAddress": {"address": email}} for email in cc_emails]

    email_payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML",
                "content": body.replace('\n', '<br>')
            },
            "toRecipients": to_recipients,
            "ccRecipients": cc_recipients,
            "from": {
                "emailAddress": {
                    "address": sender_email
                }
            },
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": pdf_file,
                    "contentType": "application/pdf",
                    "contentBytes": attachment_content
                }
            ]
        },
        "saveToSentItems": "true"
    }
    st.info("✅ Email payload and attachment prepared.")

    # 4. Send the email via Graph API
    graph_endpoint = f"https://graph.microsoft.com/v1.0/users/{sender_email}/sendMail"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        st.info("Sending email via Microsoft Graph API...")
        response = requests.post(graph_endpoint, headers=headers, json=email_payload)
        response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
        st.info(f"✅ Email sent successfully (API returned status {response.status_code}).")
        return True, None # A 202 Accepted status code means success
    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code
        try:
            error_details = e.response.json()
            error_message = error_details.get("error", {}).get("message", e.response.text)
        except Exception:
            error_message = e.response.text
        full_error = f"API Error (Status {status_code}): {error_message}"
        st.error(full_error)
        return False, full_error
    except Exception as e:
        st.error(f"An unexpected error occurred: {e}")
        return False, str(e)

def validate_form(required_fields):
    """
    Validates required text fields and signature canvases.
    Returns a dictionary of missing fields.
    """
    missing_fields = {}
    for key, (label, value) in required_fields.items():
        is_signature = "signature" in key
        is_missing = False

        if is_signature and not is_signature_present(value.image_data):
            is_missing = True
        elif not is_signature and not value:
            is_missing = True
        
        if is_missing:
            missing_fields[key] = label
    return missing_fields

def serialize_value(val):
    if isinstance(val, (datetime.date, datetime.datetime)):
        return val.isoformat()
    return val

def draw_wrapped_text(c, text, x, y, max_width, font_name="Helvetica", font_size=12, leading=14):
    lines = simpleSplit(str(text), font_name, font_size, max_width)
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y

def is_signature_present(image_data):
    if image_data is None:
        return False
    # A blank canvas will have an alpha channel of all zeros.
    # We check if any pixel has an alpha value greater than 0.
    return np.any(image_data[:, :, 3] > 0)

def save_to_gsheet(data, worksheet_name, columns):
    # st.write(f"DEBUG: Saving to Google Sheet '{worksheet_name}' with columns:", columns)
    # st.write("DEBUG: Data to save:", data)
    client = gspread.service_account_from_dict(st.secrets["gspread_creds"])
    sheet = client.open("forms").worksheet(worksheet_name)
    row = [serialize_value(data.get(col, "")) for col in columns]
    # st.write("DEBUG: Row to append:", row)
    sheet.append_row(row)
    # st.write("DEBUG: Row appended to Google Sheet.")

def process_signature_img(signature_canvas):
    # st.write("DEBUG: Processing signature image.")

    if signature_canvas.image_data is None:
        # st.write("DEBUG: No signature image data.")
        return None

    # Convert NumPy array to RGBA PIL Image
    img_array = signature_canvas.image_data.astype(np.uint8)
    signature_img = Image.fromarray(img_array, mode="RGBA")

    # Create a white RGBA background
    white_bg = Image.new("RGBA", signature_img.size, "WHITE")

    # Paste signature over white background using itself as mask
    white_bg.paste(signature_img, (0, 0), signature_img)

    # Convert to RGB (removes transparency)
    final_img = white_bg.convert("RGB")

    return final_img

def save_submission_pdf(data, field_list, pdf_title, filename, operator_signature_img=None, supervisor_signature_img=None):
    # st.write("DEBUG: Generating PDF:", filename)
    c = pdf_canvas.Canvas(filename, pagesize=letter)
    width, height = letter

    # --- Title ---
    c.setFont("Helvetica-Bold", 20)
    c.drawCentredString(width / 2, height - 50, pdf_title)
    y = height - 80

    # --- Draw a line under the title ---
    c.setLineWidth(1)
    c.line(72, y, width - 72, y)
    y -= 24

    # --- Fields with bold labels ---
    c.setFont("Helvetica", 14)
    
    wrapped_text_fields = [
        "explanation_of_incident", 
        "reason_for_non_immediate_report", 
        "incident_type_other",
        "pay_explanation",
        "traffic_location"
    ]
    
    for label, key in field_list:
        value = data.get(key, "")
        if key in wrapped_text_fields:
            c.setFont("Helvetica-Bold", 12)
            c.drawString(72, y, f"{label}:")
            y -= 16
            c.setFont("Helvetica", 12)
            y = draw_wrapped_text(c, value, 90, y, max_width=450)
            y -= 10
        else:
            c.setFont("Helvetica-Bold", 12)
            c.drawString(72, y, f"{label}:")
            c.setFont("Helvetica", 12)
            c.drawString(250, y, str(value))
            y -= 20        
        if y < 100:
            c.showPage()
            y = height - 72
            c.setFont("Helvetica", 14)
            
    # --- Signatures (optional) ---
    if operator_signature_img is not None or supervisor_signature_img is not None:
        if y < 350:
            c.showPage()
            y = height - 72
            c.setFont("Helvetica", 14)

        pdf_sig_width = 400
        pdf_sig_height = int(pdf_sig_width * (150 / 600))

        if operator_signature_img is not None:
            c.setFont("Helvetica-Bold", 12)
            c.drawString(72, y, "Operator Signature:")
            processed_img = process_signature_img(operator_signature_img)
            if processed_img:
                image_bottom_y = y - 15 - pdf_sig_height
                smooth_img = processed_img.resize((pdf_sig_width, pdf_sig_height), Image.LANCZOS)
                buf = io.BytesIO()
                smooth_img.save(buf, format="PNG")
                buf.seek(0)
                img_reader = ImageReader(buf)
                c.drawImage(img_reader, 72, image_bottom_y, width=pdf_sig_width, height=pdf_sig_height, mask='auto')
                y = image_bottom_y - 30

        if supervisor_signature_img is not None:
            c.setFont("Helvetica-Bold", 12)
            c.drawString(72, y, "Supervisor Signature:")
            processed_img = process_signature_img(supervisor_signature_img)
            if processed_img:
                image_bottom_y = y - 15 - pdf_sig_height
                smooth_img = processed_img.resize((pdf_sig_width, pdf_sig_height), Image.LANCZOS)
                buf = io.BytesIO()
                smooth_img.save(buf, format="PNG")
                buf.seek(0)
                img_reader = ImageReader(buf)
                c.drawImage(img_reader, 72, image_bottom_y, width=pdf_sig_width, height=pdf_sig_height, mask='auto')
                y = image_bottom_y - 3
    c.save()
    # st.write("DEBUG: PDF saved:", filename)
    return filename

incident_field_list = [
    ("Date", "date"),
    ("Time", "time"),
    ("AM/PM", "am_pm1"),
    ("Brief #", "brief"),
    ("Operator Name", "operator_name"),
    ("Vehicle #", "vehicle"),
    ("Operator ID", "operator_id"),
    ("Route #", "route"),
    ("Depot", "depot"),
    ("Run #", "run"),
    ("Report Submitted To", "report_submitted_to"),
    ("Incident Type", "incident_type"),
    ("Incident Type Other", "incident_type_other"),
    ("Reported Immediately", "reported_immediately"),
    ("Reported to Dispatcher", "reported_to_dispatcher"),
    ("Reason for Non-Immediate Report", "reason_for_non_immediate_report"),
    ("SQM Responded", "sqm_respond_to_incident"),
    ("Responding SQM", "responding_sqm"),
    ("Date Incident Occurred", "date_incident_occurred"),
    ("Date Incident Reported", "date_incident_reported"),
    ("Time Incident Occurred", "time_incident_occurred"),
    ("AM/PM", "am_pm2"),
    ("Time Incident Reported", "time_incident_reported"),
    ("AM/PM", "am_pm3"),
    ("No Actual Date/Time", "no_actual_date_and_time"),
    ("Late Report", "late_report"),
    ("Incident Location", "incident_location"),
    ("Passenger Name", "passenger_name"),
    ("Passenger ID/Seat #", "passenger_id"),
    ("Explanation of Incident", "explanation_of_incident"),
    ("Signing SQM Name", "signed_sqm_name"),
    ("Date Submitted", "date_submitted"),
]

pay_field_list = [
    ("Date", "date"),
    ("Name", "name"),
    ("Run #", "run"),
    ("Bus #", "bus_number"),
    ("ID #", "id_number"),
    ("Route #", "route"),
    ("Clock In", "clock_in"),
    ("AM/PM", "am_pm1"),
    ("Scheduled Clock In", "clock_in_before"),
    ("AM/PM", "am_pm2"),
    ("Clock Out", "clock_out"),
    ("AM/PM", "am_pm3"),
    ("Actual Clock Out", "actual_clock_out"),
    ("AM/PM", "am_pm4"),
    ("Weather", "weather"),
    ("Extra Work", "extra_work"),
    ("Traffic Delay", "traffic_delay"),
    ("Incident Report", "incident_report"),
    ("Bus Exchange", "bus_exchange"),
    ("Missed Meal", "missed_meal"),
    ("Road Call", "road_call"),
    ("Traffic Location", "traffic_location"),
    ("Time Reported to Command", "time_reported_to_command"),
    ("AM/PM", "am_pm5"),
    ("Explanation", "pay_explanation"),
    ("Operator Signature Date", "pay_operator_signature_date"),
    ("Signing SQM Name", "pay_signing_sqm_name"),
    ("Supervisor Signature Date", "pay_supervisor_signature_date"),
]

# Streamlit Forms

if "form_key" not in st.session_state:
    st.session_state["form_key"] = 0
if "page" not in st.session_state:
    st.session_state["page"] = "home"
    # st.write("DEBUG: Initializing page to home.")
    
def show_incident_form():
    st.title("Operator Incident Report")
    # st.write("DEBUG: show_incident_form called.")
    if st.button("Return Home", key="incident_return_top"):
        # st.write("DEBUG: Return Home button pressed.")
        st.session_state["page"] = "home"
        st.session_state["incident_submitted"] = False
        st.rerun()
    if not st.session_state.get("incident_submitted", False):
        # st.write("DEBUG: Incident form is visible.")
        
        with st.form(key=f"incident_form_{st.session_state['form_key']}"):
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                date = st.date_input(
                    "Today's Date",
                    key="incident_date"
                )
            with col2:
                highlight_missing_field("incident_time", "incident")
                time = st.text_input(
                    "Time",
                    key="incident_time"
                )
            with col3:
                am_pm1 = st.radio(
                    "AM/PM",
                    options=["AM", "PM"],
                    horizontal=True,
                    key="incident_am_pm1"
                )
            with col4:
                highlight_missing_field("incident_brief", "incident")
                brief = st.text_input(
                    "Brief #",
                    key="incident_brief"
                )

            col5, col6, col7 = st.columns(3)
            with col5:
                highlight_missing_field("incident_operator_name", "incident")
                operator_name = st.text_input(
                    "Operator Name",
                    key="incident_operator_name"
                )
                highlight_missing_field("incident_vehicle", "incident")
                vehicle = st.text_input(
                    "Vehicle #",
                    key="incident_vehicle"
                )
            with col6:
                highlight_missing_field("incident_operator_id", "incident")
                operator_id = st.text_input(
                    "Operator ID",
                    key="incident_operator_id"
                )
                highlight_missing_field("incident_route", "incident")
                route = st.text_input(
                    "Route #",
                    key="incident_route"
                )
            with col7:
                highlight_missing_field("incident_depot", "incident")
                depot = st.text_input(
                    "Depot",
                    key="incident_depot"
                )
                highlight_missing_field("incident_run", "incident")
                run = st.text_input(
                    "Run #",
                    key="incident_run"
                )

            report_submitted_to = st.radio(
                "Report Submitted To",
                options=["SQM", "Dispatch Window", "Safety Dept"],
                horizontal=True,
                key="incident_report_submitted_to"
            )

            incident_type = st.radio(
                "Incident Type",
                options=[
                    "Passenger Accident", "Passenger Incident", "Passenger Injury", "MVA",
                    "Vehicle Damage", "Passenger Complaint", "No Damage Vehicle Incident Report", "Other"
                ],
                horizontal=True,
                key="incident_type"
            )

            incident_type_other = st.text_area(
                "If Other, please specify",
                key="incident_type_other"
            )

            col8, col9 = st.columns(2)
            with col8:
                reported_immediately = st.radio(
                    "Was incident reported immediately?",
                    options=["Yes", "No"],
                    horizontal=True,
                    key="incident_reported_immediately"
                )
            with col9:
                reported_to_dispatcher = st.text_input(
                    "Reported to Dispatcher (Name)",
                    key="incident_reported_to_dispatcher"
                )

            reason_for_non_immediate_report = st.text_area(
                "I did not report this incident immediately because:",
                key="incident_reason_for_non_immediate_report"
            )

            col10, col11 = st.columns(2)
            with col10:
                sqm_respond_to_incident = st.radio(
                    "Did an SQM respond to this Incident?",
                    options=["Yes", "No"],
                    horizontal=True,
                    key="incident_sqm_respond_to_incident"
                )
            with col11:
                responding_sqm = st.text_input(
                    "SQM",
                    key="incident_responding_sqm"
                )

            col12, col13, col14, col15 = st.columns(4)
            with col12:
                date_incident_occurred = st.date_input(
                    "Date incident occurred",
                    key="incident_date_incident_occurred"
                )
                date_incident_reported = st.date_input(
                    "Date incident reported",
                    key="incident_date_incident_reported"
                )
            with col13:
                time_incident_occurred = st.text_input(
                    "Time incident occurred",
                    key="incident_time_incident_occurred"
                )
                time_incident_reported = st.text_input(
                    "Time incident reported",
                    key="incident_time_incident_reported"
                )
            with col14:
                am_pm2 = st.radio(
                    "AM/PM",
                    options=["AM", "PM"],
                    horizontal=True,
                    key="incident_am_pm2"
                )
                am_pm3 = st.radio(
                    "AM/PM",
                    options=["AM", "PM"],
                    horizontal=True,
                    key="incident_am_pm3"
                )
            with col15:
                no_actual_date_and_time = st.checkbox(
                    "Do not have actual date and time.",
                    key="incident_no_actual_date_and_time"
                )
                late_report = st.checkbox(
                    "This is a late report.",
                    key="incident_late_report"
                )

            highlight_missing_field("incident_location", "incident")
            incident_location = st.text_input(
                "Location of incident",
                key="incident_location"
            )

            st.write("Complete a separate incident report for each passenger affected by this incident.")
            col16, col17 = st.columns(2)
            with col16:
                passenger_name = st.text_input(
                    "Passenger Name",
                    key="incident_passenger_name"
                )
            with col17:
                passenger_id = st.text_input(
                    "Passenger ID/Seat #",
                    key="incident_passenger_id"
                )

            highlight_missing_field("explanation_of_incident", "incident")
            explanation_of_incident = st.text_area(
                "Explain what happened",
                key="explanation_of_incident"
            )
            st.write("Operator Signature below:")
            highlight_missing_field("operator_signature", "incident")
            operator_signature = st_canvas(
                fill_color="rgba(255, 165, 0, 0.3)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#ffffff",
                height=150,
                width=600,
                drawing_mode="freedraw",
                key="incident_operator_signature",
            )

            highlight_missing_field("incident_signed_sqm_name", "incident")
            signed_sqm_name = st.text_input(
                "Signing SQM Name",
                key="incident_signed_sqm_name"
            )

            st.write("Supervisor Signature below:")
            highlight_missing_field("supervisor_signature", "incident")
            supervisor_signature = st_canvas(
                fill_color="rgba(255, 165, 0, 0.3)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#ffffff",
                height=150,
                width=600,
                drawing_mode="freedraw",
                key="incident_supervisor_signature",
            )

            date_submitted = st.date_input(
                "Date Submitted",
                key="incident_date_submitted"
            )
            
            incident_required_fields = {
                "incident_time": ("Time", time),
                "incident_brief": ("Brief #", brief),
                "incident_operator_name": ("Operator Name", operator_name),
                "incident_vehicle": ("Vehicle #", vehicle),
                "incident_operator_id": ("Operator ID", operator_id),
                "incident_route": ("Route #", route),
                "incident_depot": ("Depot", depot),
                "incident_run": ("Run #", run),
                "incident_location": ("Location of incident", incident_location),
                "explanation_of_incident": ("Explain what happened", explanation_of_incident),
                "incident_signed_sqm_name": ("Signed SQM Name", signed_sqm_name),
                "operator_signature": ("Operator Signature", operator_signature),
                "supervisor_signature": ("Supervisor Signature", supervisor_signature),
            }
            
            
            
            display_submit_button_error("incident", incident_required_fields)
            
            submitted = st.form_submit_button("Submit Incident Report")
            if submitted:
                
                missing_fields = validate_form(incident_required_fields)
                
                if missing_fields:
                    st.session_state['missing_incident_fields'] = list(missing_fields.keys())
                    st.rerun()
                else:
                    # Clear missing fields on success
                    st.session_state['missing_incident_fields'] = []
                    st.session_state['submit_error_incident'] = ""

                incident_form_data = {
                    "date": date,
                    "time": time,
                    "am_pm1": am_pm1,
                    "brief": brief,
                    "operator_name": operator_name,
                    "operator_id": operator_id,
                    "depot": depot,
                    "vehicle": vehicle,
                    "route": route,
                    "run": run,
                    "report_submitted_to": report_submitted_to,
                    "incident_type": incident_type,
                    "incident_type_other": incident_type_other,
                    "reported_immediately": reported_immediately,
                    "reported_to_dispatcher": reported_to_dispatcher,
                    "reason_for_non_immediate_report": reason_for_non_immediate_report,
                    "sqm_respond_to_incident": sqm_respond_to_incident,
                    "responding_sqm": responding_sqm,
                    "date_incident_occurred": date_incident_occurred,
                    "date_incident_reported": date_incident_reported,
                    "time_incident_occurred": time_incident_occurred,
                    "am_pm2": am_pm2,
                    "time_incident_reported": time_incident_reported,
                    "am_pm3": am_pm3,
                    "no_actual_date_and_time": no_actual_date_and_time,
                    "late_report": late_report,
                    "incident_location": incident_location,
                    "passenger_name": passenger_name,
                    "passenger_id": passenger_id,
                    "explanation_of_incident": explanation_of_incident,
                    "signed_sqm_name": signed_sqm_name,
                    "date_submitted": date_submitted,
                }
                # st.write("DEBUG: incident_form_data:", incident_form_data)

                incident_columns = [
                    "date", "time", "am_pm1", "brief", "operator_name", "operator_id", "depot", "vehicle", 
                    "route", "run", "report_submitted_to", "incident_type", "incident_type_other", 
                    "reported_immediately", "reported_to_dispatcher", "reason_for_non_immediate_report", 
                    "sqm_respond_to_incident", "responding_sqm", "date_incident_occurred", 
                    "date_incident_reported", "time_incident_occurred", "am_pm2", "time_incident_reported", 
                    "am_pm3", "no_actual_date_and_time", "late_report", "incident_location", 
                    "passenger_name", "passenger_id", "explanation_of_incident", "signed_sqm_name", 
                    "date_submitted"
                ]

                try:
                    save_to_gsheet(incident_form_data, worksheet_name="Incident Reports", columns=incident_columns)
                    # st.write("DEBUG: Saved incident to Google Sheet.")
                except Exception as e:
                    st.error(f"Failed to save to Google Sheet: {e}")
                    # st.write("DEBUG: Google Sheet error:", e)

                # 2. Generate PDF
                try:
                    # For incident report
                    filename = f"incident_{incident_form_data['operator_name']}_{incident_form_data['date']}_for_brief_{incident_form_data['brief']}.pdf"
                    save_submission_pdf(
                        incident_form_data,
                        incident_field_list,
                        "Operator Incident Report",
                        filename,
                        operator_signature_img=operator_signature,
                        supervisor_signature_img=supervisor_signature
                    )
                    # st.write("DEBUG: PDF generated:", filename)
                except Exception as e:
                    st.error(f"Failed to generate PDF: {e}")
                    st.error(f"Failed to generate PDF: {e}")
                    filename = None

                # 3. Send Email (only if PDF was created)
                if filename:
                    subject = f"Incident Report: {incident_form_data['operator_name']} on {incident_form_data['date']} for Brief # {incident_form_data['brief']}"
                    body = f"""
                    An incident report has been submitted.

                    Operator: {incident_form_data['operator_name']}
                    Date: {incident_form_data['date']}
                    Brief: {incident_form_data['brief']}
                    
                    See attached PDF for details.
                    """
                    try:
                        success, error = send_pdf_email(
                            filename,
                            subject,
                            body,
                            to_email=st.secrets.get("to_emails"),
                            cc_emails=st.secrets.get("cc_emails")
                        )
                        st.write(f"DEBUG: Email send function returned: success={success}, error='{error}'")
                        if not success:
                            st.error(f"Failed to send email: {error}")
                    except Exception as e:
                        st.error(f"An exception occurred while trying to send email: {e}")
                        # st.write("DEBUG: Email error:", e)
                st.session_state["incident_form_data"] = incident_form_data
                st.session_state["incident_submitted"] = True
                # st.write("DEBUG: Setting incident_submitted to True and rerunning.")
                st.warning("DEBUG: Rerun is disabled. The app has finished processing this submission.")
                # st.rerun()
            
        # Clear button (inside form, but outside submit logic)
        if st.button("Clear", key="incident_clear_bottom"):
            # st.write("DEBUG: Incident form Clear button pressed.")
            for key in list(st.session_state.keys()):
                if key not in ("form_key", "page"):
                    del st.session_state[key]
            st.session_state["form_key"] += 1
            st.session_state["incident_submitted"] = False
            st.rerun()
            
    else:
        st.success("Incident Report submitted!")
        # st.write("DEBUG: Incident report submitted message shown.")
        if st.button("Clear", key="incident_clear_bottom"):
            # st.write("DEBUG: Incident form Clear button pressed (after submission).")
            for key in list(st.session_state.keys()):
                if key not in ("form_key", "page"):
                    del st.session_state[key]
            st.session_state["form_key"] += 1
            st.session_state["incident_submitted"] = False
            st.rerun()        
            

def show_pay_exception_form():
    st.title("Operator Pay Exception Form")
    # st.write("DEBUG: show_pay_exception_form called.")
    if st.button("Return Home", key="pay_exception_return_top"):
        # st.write("DEBUG: Pay Exception Return Home button pressed.")
        st.session_state["page"] = "home"
        st.session_state["pay_exception_submitted"] = False
        st.rerun()
    if not st.session_state.get("pay_exception_submitted", False):
        # st.write("DEBUG: Pay Exception form is visible.")
        
        with st.form(key=f"pay_exception_form_{st.session_state['form_key']}"):
            col1, col2 = st.columns(2)
            with col1:
                date = st.date_input("Date", key="pay_date")
                highlight_missing_field("pay_name", "pay_exception")
                name = st.text_input("Name", key="pay_name")
                highlight_missing_field("pay_run", "pay_exception")
                run = st.text_input("Run #", key="pay_run")
            with col2:
                highlight_missing_field("pay_bus_number", "pay_exception")
                bus_number = st.text_input("Bus #", key="pay_bus_number")
                highlight_missing_field("pay_id_number", "pay_exception")
                id_number = st.text_input("ID #", key="pay_id_number")
                highlight_missing_field("pay_route", "pay_exception")
                route = st.text_input("Route #", key="pay_route")

            col3, col4, col5, col6 = st.columns(4)
            with col3:
                clock_in = st.text_input("Clock In", key="pay_clock_in")
                clock_in_before = st.text_input(
                    "Scheduled Clock In (Only fill out if Operator is asked to report before)",
                    key="pay_scheduled_clock_in"
                )
            with col4:
                am_pm1 = st.radio("AM/PM", options=["AM", "PM"], horizontal=True, key="pay_am_pm1")
                am_pm2 = st.radio("AM/PM", options=["AM", "PM"], horizontal=True, key="pay_am_pm2")
            with col5:
                clock_out = st.text_input("Clock Out", key="pay_clock_out")
                actual_clock_out = st.text_input("Actual Clock Out", key="pay_actual_clock_out")          
            with col6:
                am_pm3 = st.radio("AM/PM", options=["AM", "PM"], horizontal=True, key="pay_am_pm3")
                am_pm4 = st.radio("AM/PM", options=["AM", "PM"], horizontal=True, key="pay_am_pm4")
            
            st.write('Reason for the Exception;') 
            st.write('Please Note…If you are pulling in late, you need a reason. If your noting "traffic" we need a description of what route, what street and time of the traffic you encountered. Please be advised all late pull ins MUST be texted to Command Center using the Clever system.')
            st.write("*Note that all late pull-in's will be validated in the CAD system.")
            
            col7, col8, col9 = st.columns(3)
            with col7:
                weather = st.checkbox("Weather", key="pay_weather")
                extra_work = st.checkbox("Extra Work", key="pay_extra_work")
                traffic_delay = st.checkbox("Traffic Delay", key="pay_traffic_delay")
            with col8:
                incident_report = st.checkbox("Acc./Incident Report", key="pay_incident_report")
                bus_exchange = st.checkbox("Bus Exchange", key="pay_bus_exchange")
            with col9:
                missed_meal = st.checkbox("Missed Meal", key="pay_missed_meal")
                road_call = st.checkbox("Road Call", key="pay_road_call")
            traffic_location = st.text_input("Location of Traffic", key="pay_traffic_location")
            col10, col11 = st.columns(2)
            with col10:
                time_reported_to_command = st.text_input("Time Reported to Center", key="pay_time_reported_to_command")
            with col11:
                am_pm5 = st.radio("AM/PM", options=["AM", "PM"], horizontal=True, key="pay_am_pm5")
                
            highlight_missing_field("pay_explanation", "pay_exception")
            pay_explanation = st.text_area("Explanation(Must be filled in.)", key="pay_explanation", height=150)
            
            st.write("Operator Signature below:")
            pay_operator_signature = st_canvas(
                fill_color="rgba(255, 165, 0, 0.3)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#ffffff",
                height=150,
                width=600,
                drawing_mode="freedraw",
                key="pay_operator_signature",
            )

            pay_operator_signature_date = st.date_input("Date", key="pay_operator_signature_date")

            st.write("Supervisor Signature below:")
            pay_supervisor_signature = st_canvas(
                fill_color="rgba(255, 165, 0, 0.3)",
                stroke_width=2,
                stroke_color="#000000",
                background_color="#ffffff",
                height=150,
                width=600,
                drawing_mode="freedraw",
                key="pay_supervisor_signature",
            )
            
            highlight_missing_field("pay_signing_sqm_name", "pay_exception")
            pay_supervisor_signature_name = st.text_input("Signing SQM Name", key="pay_signing_sqm_name")
            pay_supervisor_signature_date = st.date_input("Date", key="pay_supervisor_signature_date")
            
            # st.write("DEBUG: Pay Exception form submitted.")
            pay_required_fields = {
                "pay_date": ("Date", date),
                "pay_name": ("Name", name),
                "pay_run": ("Run #", run),
                "pay_bus_number": ("Bus #", bus_number),
                "pay_id_number": ("ID #", id_number),
                "pay_route": ("Route #", route),
                "pay_explanation": ("Explanation", pay_explanation),
                "pay_operator_signature_date": ("Operator Signature Date", pay_operator_signature_date),
                "pay_signing_sqm_name": ("Signing SQM Name", pay_supervisor_signature_name),
                "pay_supervisor_signature_date": ("Supervisor Signature Date", pay_supervisor_signature_date),
                "pay_operator_signature": ("Operator Signature", pay_operator_signature),
                "pay_supervisor_signature": ("Supervisor Signature", pay_supervisor_signature),
            }
            
            display_submit_button_error("pay_exception", pay_required_fields)
            
            submitted = st.form_submit_button("Submit Pay Exception Form")
            
            if submitted:                
                missing_fields = validate_form(pay_required_fields)
                
                if missing_fields:
                    st.session_state['missing_pay_exception_fields'] = list(missing_fields.keys())
                    st.rerun()
                else:
                    st.session_state['missing_pay_exception_fields'] = []
                    pay_form_data = {
                        "date": date,
                        "name": name,
                        "run": run,
                        "bus_number": bus_number,
                        "id_number": id_number,
                        "route": route,
                        "clock_in": clock_in,
                        "am_pm1": am_pm1,
                        "clock_in_before": clock_in_before,
                        "am_pm2": am_pm2,
                        "clock_out": clock_out,
                        "am_pm3": am_pm3,
                        "actual_clock_out": actual_clock_out,
                        "am_pm4": am_pm4,
                        "weather": weather,
                        "extra_work": extra_work,
                        "traffic_delay": traffic_delay,
                        "incident_report": incident_report,
                        "bus_exchange": bus_exchange,
                        "missed_meal": missed_meal,
                        "road_call": road_call,
                        "traffic_location": traffic_location,
                        "time_reported_to_command": time_reported_to_command,
                        "am_pm5": am_pm5,
                        "pay_explanation": pay_explanation,
                        "pay_operator_signature_date": pay_operator_signature_date,
                        "pay_signing_sqm_name": pay_supervisor_signature_name,
                        "pay_supervisor_signature_date": pay_supervisor_signature_date,
                    }
                    # st.write("DEBUG: pay_form_data:", pay_form_data)
                    pay_columns = [
                        "date", "name", "run", "bus_number", "id_number", "route", "clock_in", "am_pm1",
                        "clock_in_before","am_pm2", "clock_out", "am_pm3", "actual_clock_out",  "am_pm4", 
                        "weather", "extra_work", "traffic_delay", "incident_report", "bus_exchange", "missed_meal", 
                        "road_call", "traffic_location", "time_reported_to_command", "am_pm5", "pay_explanation",
                        "pay_operator_signature_date", "pay_signing_sqm_name", "pay_supervisor_signature_date",
                    ]
                    # st.write("DEBUG: pay_columns:", pay_columns)
                    try:
                        save_to_gsheet(pay_form_data, worksheet_name="Pay Exception Forms", columns=pay_columns)
                        # st.write("DEBUG: Saved pay exception to Google Sheet.")
                    except Exception as e:
                        st.error(f"Failed to save to Google Sheet: {e}")
                        # st.write("DEBUG: Google Sheet error:", e)
                        
                    try:
                        # For pay exception
                        filename = f"pay_exception_{pay_form_data['name']}_{pay_form_data['date']}.pdf"
                        save_submission_pdf(
                            pay_form_data,
                            pay_field_list,
                            "Operator Pay Exception Form",
                            filename,
                            operator_signature_img=pay_operator_signature,
                            supervisor_signature_img=pay_supervisor_signature
                        )
                        # st.write("DEBUG: PDF generated:", filename)
                    except Exception as e:
                        st.error(f"Failed to generate PDF: {e}")
                        # st.write("DEBUG: PDF error:", e)
                        filename = None
                    
                    # Send Email (only if PDF was created)    
                    if filename:
                        subject = f"Pay Exception Form: {pay_form_data['name']} on {pay_form_data['date']}"
                        body = f"""
                        A pay exception form has been submitted.

                        Operator: {pay_form_data['name']}
                        Date: {pay_form_data['date']}
                        Run #: {pay_form_data['run']}

                        See attached PDF for details.
                        """
                        try:
                            success, error = send_pdf_email(
                                filename,
                                subject,
                                body,
                                to_email=st.secrets.get("to_emails"),
                                cc_emails=st.secrets.get("cc_emails")
                            )
                            st.write(f"DEBUG: Email send function returned: success={success}, error='{error}'")
                            if not success:
                                st.error(f"Failed to send email: {error}")
                        except Exception as e:
                            st.error(f"An exception occurred while trying to send email: {e}")
                            # st.write("DEBUG: Email error:", e)
                    st.session_state["pay_form_data"] = pay_form_data
                    st.session_state["pay_exception_submitted"] = True
                    # st.write("DEBUG: Setting pay_exception_submitted to True and rerunning.")
                    st.warning("DEBUG: Rerun is disabled. The app has finished processing this submission.")
                    # st.rerun()
            
        if st.button("Clear", key="pay_exception_clear_bottom"):
            # st.write("DEBUG: Pay Exception form Clear button pressed.")
            for key in list(st.session_state.keys()):
                if key not in ("form_key", "page"):
                    del st.session_state[key]
            st.session_state["form_key"] += 1
            st.session_state["pay_exception_submitted"] = False
            st.rerun()
            
    else:
        st.success("Pay Exception Form submitted!")
        # st.write("DEBUG: Pay Exception form submitted message shown.")
        if st.button("Clear", key="pay_exception_clear_bottom"):
            # st.write("DEBUG: Pay Exception form Clear button pressed (after submission).")
            for key in list(st.session_state.keys()):
                if key not in ("form_key", "page"):
                    del st.session_state[key]
            st.session_state["form_key"] += 1
            st.session_state["pay_exception_submitted"] = False
            st.rerun()      


# App Navigation

if "page" not in st.session_state:
    st.session_state["page"] = "home"
    # st.write("DEBUG: Initializing page to home.")
    
if st.session_state["page"] == "home":
    st.title("Welcome")
    st.write("Please select a form to fill out:")
    # st.write("DEBUG: Home page displayed.")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Operator Incident Report"):
            # st.write("DEBUG: Operator Incident Report button pressed.")
            st.session_state["page"] = "incident"
            st.rerun()
    with col2:
        if st.button("Operator Pay Exception Form"):
            # st.write("DEBUG: Operator Pay Exception Form button pressed.")
            st.session_state["page"] = "pay_exception"
            st.rerun()
            
elif st.session_state["page"] == "incident":
    # st.write("DEBUG: Navigating to incident form.")
    show_incident_form()
elif st.session_state["page"] == "pay_exception":
    # st.write("DEBUG: Navigating to pay exception form.")
    show_pay_exception_form()
