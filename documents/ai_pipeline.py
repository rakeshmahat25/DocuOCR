import os
import json
import re
import logging
from datetime import datetime
from django.conf import settings
from django.core.files.base import ContentFile
import fitz
from .ocr import ocr_image
from PIL import Image
from core.models import Workspace
from .models import Workflow, ExtractionField, Document, DocumentPage, DocumentFieldResult
from .preprocessing import preprocess_image
from .fraud_detector import run_fraud_checks

logger = logging.getLogger(__name__)

def process_document(document_id):
    try:
        document = Document.objects.select_related("workflow", "workspace", "workspace__tenant").get(id=document_id)
    except Document.DoesNotExist:
        logger.error(f"Document {document_id} not found.")
        return

    document.status = Document.STATUS_PREPROCESSING
    document.save(update_fields=["status"])

    pages_images = []
    file_path = document.file.path

    try:
        if document.file_type.lower() == "pdf":
            try:
                doc = fitz.open(file_path)
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150)
                    img_data = pix.tobytes("png")

                    page_img_name = f"doc_{document.id}_page_{i+1}.png"
                    temp_img_path = os.path.join(settings.MEDIA_ROOT, "temp_pages")
                    os.makedirs(temp_img_path, exist_ok=True)
                    temp_file = os.path.join(temp_img_path, page_img_name)

                    with open(temp_file, "wb") as f:
                        f.write(img_data)

                    processed_img_name = f"proc_doc_{document.id}_page_{i+1}.png"
                    processed_dir = os.path.join(settings.MEDIA_ROOT, "document_pages")
                    os.makedirs(processed_dir, exist_ok=True)
                    processed_file = os.path.join(processed_dir, processed_img_name)

                    preprocess_image(temp_file, processed_file)

                    page_text = page.get_text() or ""

                    doc_page = DocumentPage.objects.create(
                        document=document,
                        page_number=i+1,
                        raw_text=page_text
                    )

                    with open(processed_file, "rb") as f:
                        doc_page.image.save(processed_img_name, ContentFile(f.read()), save=True)

                    pages_images.append(doc_page)

                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                doc.close()
            except Exception as pdf_error:
                logger.warning(f"PyMuPDF failed to parse PDF {file_path}: {pdf_error}. Creating empty page.")
                doc_page = DocumentPage.objects.create(
                    document=document,
                    page_number=1,
                    raw_text=""
                )
                pages_images.append(doc_page)
        else:
            processed_img_name = f"proc_doc_{document.id}_page_1.png"
            processed_dir = os.path.join(settings.MEDIA_ROOT, "document_pages")
            os.makedirs(processed_dir, exist_ok=True)
            processed_file = os.path.join(processed_dir, processed_img_name)

            preprocess_image(file_path, processed_file)
            ocr_text = ocr_image(processed_file)
            doc_page = DocumentPage.objects.create(
                document=document,
                page_number=1,
                raw_text=ocr_text or ""
            )
            with open(processed_file, "rb") as f:
                doc_page.image.save(processed_img_name, ContentFile(f.read()), save=True)

            pages_images.append(doc_page)

    except Exception as e:
        logger.error(f"Error rendering document pages: {e}")
        document.status = Document.STATUS_FAILED
        document.save(update_fields=["status"])
        return

    document.raw_text = "\n--- PAGE BREAK ---\n".join([p.raw_text for p in pages_images])
    document.save(update_fields=["raw_text"])

    document.status = Document.STATUS_EXTRACTING
    document.save(update_fields=["status"])

    workflow = document.workflow
    if not workflow:
        doc_type_detected = auto_detect_document_type(document)
        workflow = Workflow.objects.filter(workspace=document.workspace, document_type=doc_type_detected, is_active=True).first()
        if not workflow:
            workflow = create_default_workflow(document.workspace, doc_type_detected)

        document.workflow = workflow
        document.save(update_fields=["workflow"])

    fields_to_extract = list(workflow.fields.all())

    api_key = document.workspace.tenant.gemini_api_key or settings.GEMINI_API_KEY
    extraction_results = None

    if api_key:
        extraction_results = run_gemini_extraction(api_key, document, pages_images, fields_to_extract)

    if not extraction_results:
        extraction_results = run_local_simulator_extraction(document, pages_images, fields_to_extract)

    document.extracted_json = extraction_results
    document.confidence_score = extraction_results.get("confidence_score", 0.90)
    document.save(update_fields=["extracted_json", "confidence_score"])

    document.status = Document.STATUS_VALIDATING
    document.save(update_fields=["status"])

    validation_passed = evaluate_validation_rules(document, fields_to_extract, extraction_results.get("fields", {}))

    run_fraud_checks(document)

    needs_review = (
        document.confidence_score < 0.85 or
        not validation_passed or
        document.fraud_status in [Document.FRAUD_REVIEW, Document.FRAUD_FAILED] or
        document.validation_status in [Document.VALIDATION_WARNING, Document.VALIDATION_FAILED]
    )

    if needs_review:
        document.status = Document.STATUS_HUMAN_REVIEW
    else:
        document.status = Document.STATUS_COMPLETED
        if document.validation_status == Document.VALIDATION_PENDING:
            document.validation_status = Document.VALIDATION_VERIFIED

    document.processed_at = datetime.now()
    document.save(update_fields=["status", "validation_status", "processed_at"])

    trigger_websocket_update(document)
    trigger_webhooks(document)


def auto_detect_document_type(document):
    """Multi-class classifier for detecting document types from filename keywords.
    Supports Nepali government docs, bank docs, educational institute docs, and more.
    """
    name = document.name.lower()

    # ── Nepali Citizenship & Identity ──
    if any(kwd in name for kwd in [
        "nagarikta", "citizenship", "nepali_id", "nepali_identity"
    ]):
        return "nepali_citizenship"
    if any(kwd in name for kwd in [
        "nepali_passport", "nepal_passport"
    ]):
        return "nepali_passport"
    if any(kwd in name for kwd in [
        "voter", "vote", "election", "nirwachan", "mata_data"
    ]):
        return "nepali_voter_id"
    if any(kwd in name for kwd in [
        "pan_card", "pan_", "tax_registration", "pan_number"
    ]):
        return "nepali_pan"
    if any(kwd in name for kwd in [
        "driving", "driver", "license", "driving_license", "driving-license",
        "savari", "chalak", "driving_permit"
    ]):
        return "driving_license"

    # ── Nepali Government Documents ──
    if any(kwd in name for kwd in [
        "lalmohan", "land_ownership", "land_certificate", "jagga", "bhumi",
        "property_title", "rokkha", "land_registration"
    ]):
        return "land_ownership"
    if any(kwd in name for kwd in [
        "vehicle_registration", "blue_book", "vehicle", "savari_registration",
        "number_plate", "registration_book"
    ]):
        return "vehicle_registration"
    if any(kwd in name for kwd in [
        "birth", "janma", "birth_certificate"
    ]):
        return "birth_certificate"
    if any(kwd in name for kwd in [
        "marriage", "wedding", "bihe", "marriage_certificate", "marriage_license"
    ]):
        return "marriage_certificate"
    if any(kwd in name for kwd in [
        "tax_clearance", "tax_certificate", "tax_payment", "rajaswa",
        "tax_receipt", "income_tax"
    ]):
        return "tax_certificate"
    if any(kwd in name for kwd in [
        "permit", "license", "naksa", "map", "construction_permit",
        "business_registration", "company_registration", "firma",
        "trade_license", "garbage", "pass", "gate_pass"
    ]):
        return "permit_license"

    # ── Passport / KYC (generic) ──
    if any(kwd in name for kwd in [
        "passport", "national_id", "kyc", "id_card", "identity", "aadhaar"
    ]):
        return "kyc_id"

    # ── Bank Documents ──
    if any(kwd in name for kwd in [
        "statement", "bank_stmt", "bank_statement", "account_statement"
    ]):
        return "bank_statement"
    if any(kwd in name for kwd in [
        "cheque", "check", "checkbook", "cheque_book", "cheque_image"
    ]):
        return "cheque"
    if any(kwd in name for kwd in [
        "deposit", "deposit_slip", "bank_slip", "payment_slip", "cash_slip",
        "jama", "bank_voucher"
    ]):
        return "deposit_slip"
    if any(kwd in name for kwd in [
        "loan", "loan_agreement", "loan_application", "loan_sanction",
        "loan_statement", "kisti"
    ]):
        return "loan_agreement"
    if any(kwd in name for kwd in [
        "credit_card", "card_statement", "cc_statement"
    ]):
        return "credit_card_statement"
    if any(kwd in name for kwd in [
        "bank_certificate", "balance_certificate", "bank_balance",
        "account_certificate"
    ]):
        return "bank_certificate"
    if any(kwd in name for kwd in [
        "transaction", "receipt", "transfer", "payment_receipt",
        "transaction_receipt", "remittance", "send_money", "bank_transfer",
        "neft", "rtgs", "imps", "payment_confirm"
    ]):
        return "transaction_receipt"

    # ── Institute / Educational Documents ──
    if any(kwd in name for kwd in [
        "transcript", "academic_record", "marksheet", "grade_card",
        "result", "scoresheet", "academic_transcript"
    ]):
        return "transcript"
    if any(kwd in name for kwd in [
        "certificate", "completion", "slc", "see", "school_leaving",
        "graduation", "diploma_certificate", "provisional"
    ]):
        return "certificate"
    if any(kwd in name for kwd in [
        "admit", "admit_card", "hall_ticket", "exam_permit", "entry_pass"
    ]):
        return "admit_card"
    if any(kwd in name for kwd in [
        "degree", "bachelor", "master", "phd", "masters", "bachelors",
        "graduate", "postgraduate", "degree_certificate"
    ]):
        return "degree"
    if any(kwd in name for kwd in [
        "character", "conduct", "character_certificate"
    ]):
        return "character_certificate"
    if any(kwd in name for kwd in [
        "recommendation", "referral", "letter_of_recommendation"
    ]):
        return "recommendation_letter"
    if any(kwd in name for kwd in [
        "student_id", "employee_id", "id-card", "identity_card", "college_id"
    ]):
        return "id_card"
    if any(kwd in name for kwd in [
        "registration", "enrollment", "admission", "registraton_certificate",
        "student_registration"
    ]):
        return "registration_certificate"

    # ── Commercial / Business ──
    if any(kwd in name for kwd in ["invoice", "bill", "tax_invoice"]):
        return "invoice"
    if any(kwd in name for kwd in [
        "receipt", "payment_receipt", "cash_memo"
    ]):
        return "receipt"
    if any(kwd in name for kwd in [
        "contract", "agreement", "service_agreement", "nda",
        "non_disclosure", "moa", "mou", "partnership"
    ]):
        return "contract"
    if any(kwd in name for kwd in [
        "insurance", "insurance_policy", "insurance_certificate",
        "claim", "bima"
    ]):
        return "insurance"

    # ── HR / Employment ──
    if any(kwd in name for kwd in [
        "payslip", "salary_slip", "payroll", "salary_statement",
        "wages", "payment_slip"
    ]):
        return "payslip"
    if any(kwd in name for kwd in [
        "offer_letter", "appointment", "offer-letter", "job_offer",
        "employment_letter"
    ]):
        return "offer_letter"
    if any(kwd in name for kwd in [
        "resume", "cv", "curriculum_vitae", "bio_data", "biodata"
    ]):
        return "resume"

    # ── Utilities ──
    if any(kwd in name for kwd in [
        "utility", "electricity", "water_bill", "internet_bill",
        "telephone_bill", "phone_bill", "bijuli", "khane_pani",
        "gas_bill", "electric_bill"
    ]):
        return "utility_bill"

    # ── Medical ──
    if any(kwd in name for kwd in [
        "medical", "prescription", "health_record", "diagnostic",
        "lab_report", "pathology", "xray", "mri", "ct_scan",
        "doctor", "hospital", "patient"
    ]):
        return "medical"

    return "custom"


def create_default_workflow(workspace, document_type):
    """Creates a default workflow with standard fields for rapid onboarding.
    Supports 30+ document types including Nepali, banking, and educational docs.
    """
    workflow_name = f"Default {document_type.replace('_', ' ').title()} Workflow"
    workflow = Workflow.objects.create(
        workspace=workspace,
        name=workflow_name,
        document_type=document_type
    )

    rules = {}
    fields = []

    # ── Commercial / Finance ──
    if document_type == "invoice":
        fields = [
            ("invoice_number", "Invoice Number", "text"),
            ("vendor_name", "Vendor Name", "text"),
            ("invoice_date", "Invoice Date", "date"),
            ("subtotal", "Subtotal Amount", "number"),
            ("tax_amount", "Tax Amount", "number"),
            ("total_amount", "Total Amount", "number"),
            ("due_date", "Due Date", "date"),
        ]
        rules = {"math_expression": "subtotal + tax_amount == total_amount"}

    elif document_type == "receipt":
        fields = [
            ("receipt_number", "Receipt Number", "text"),
            ("merchant_name", "Merchant Name", "text"),
            ("receipt_date", "Receipt Date", "date"),
            ("total_paid", "Total Paid", "number"),
            ("payment_method", "Payment Method", "text"),
        ]

    elif document_type == "contract":
        fields = [
            ("contract_title", "Contract Title", "text"),
            ("party_a", "Party A", "text"),
            ("party_b", "Party B", "text"),
            ("contract_date", "Contract Date", "date"),
            ("effective_date", "Effective Date", "date"),
            ("expiry_date", "Expiry Date", "date"),
            ("contract_value", "Contract Value", "number"),
        ]

    elif document_type == "insurance":
        fields = [
            ("policy_number", "Policy Number", "text"),
            ("insured_name", "Insured Name", "text"),
            ("insurance_type", "Insurance Type", "text"),
            ("premium_amount", "Premium Amount", "number"),
            ("coverage_amount", "Coverage Amount", "number"),
            ("start_date", "Start Date", "date"),
            ("expiry_date", "Expiry Date", "date"),
        ]

    elif document_type == "payslip":
        fields = [
            ("employee_name", "Employee Name", "text"),
            ("employee_id", "Employee ID", "text"),
            ("payslip_month", "Payslip Month", "text"),
            ("basic_salary", "Basic Salary", "number"),
            ("allowances", "Allowances", "number"),
            ("deductions", "Deductions", "number"),
            ("net_pay", "Net Pay", "number"),
            ("payment_date", "Payment Date", "date"),
        ]

    elif document_type == "offer_letter":
        fields = [
            ("candidate_name", "Candidate Name", "text"),
            ("position", "Position", "text"),
            ("offer_date", "Offer Date", "date"),
            ("joining_date", "Joining Date", "date"),
            ("salary", "Salary Offered", "number"),
            ("company_name", "Company Name", "text"),
        ]

    elif document_type == "resume":
        fields = [
            ("full_name", "Full Name", "text"),
            ("email", "Email", "text"),
            ("phone", "Phone", "text"),
            ("education", "Education", "text"),
            ("skills", "Skills", "text"),
            ("work_experience", "Work Experience", "text"),
        ]

    elif document_type == "utility_bill":
        fields = [
            ("utility_type", "Utility Type", "text"),
            ("customer_name", "Customer Name", "text"),
            ("customer_id", "Customer ID", "text"),
            ("bill_date", "Bill Date", "date"),
            ("due_date", "Due Date", "date"),
            ("amount_due", "Amount Due", "number"),
            ("bill_period", "Bill Period", "text"),
        ]

    # ── Nepali Citizenship & Identity ──
    elif document_type == "nepali_citizenship":
        fields = [
            ("citizenship_number", "Citizenship Number", "text"),
            ("full_name", "Full Name (Nepali + English)", "text"),
            ("father_name", "Father's Name", "text"),
            ("mother_name", "Mother's Name", "text"),
            ("spouse_name", "Spouse Name", "text"),
            ("date_of_birth", "Date of Birth (B.S.)", "date"),
            ("permanent_address", "Permanent Address", "text"),
            ("current_address", "Current Address", "text"),
            ("gender", "Gender", "text"),
            ("issue_date", "Issue Date", "date"),
            ("issued_by", "Issued By (District)", "text"),
        ]

    elif document_type == "nepali_passport":
        fields = [
            ("passport_number", "Passport Number", "text"),
            ("full_name", "Full Name", "text"),
            ("date_of_birth", "Date of Birth", "date"),
            ("place_of_birth", "Place of Birth", "text"),
            ("nationality", "Nationality", "text"),
            ("gender", "Gender", "text"),
            ("issue_date", "Issue Date", "date"),
            ("expiry_date", "Expiry Date", "date"),
            ("issued_by", "Issued By", "text"),
            ("passport_type", "Passport Type", "text"),
        ]

    elif document_type == "nepali_voter_id":
        fields = [
            ("voter_id_number", "Voter ID Number", "text"),
            ("full_name", "Full Name", "text"),
            ("father_name", "Father's Name", "text"),
            ("mother_name", "Mother's Name", "text"),
            ("date_of_birth", "Date of Birth", "date"),
            ("age", "Age", "number"),
            ("gender", "Gender", "text"),
            ("permanent_address", "Permanent Address", "text"),
            ("polling_station", "Polling Station", "text"),
            ("district", "District", "text"),
        ]

    elif document_type == "nepali_pan":
        fields = [
            ("pan_number", "PAN Number", "text"),
            ("full_name", "Full Name / Company Name", "text"),
            ("pan_type", "PAN Type (Individual/Organization)", "text"),
            ("permanent_address", "Permanent Address", "text"),
            ("issue_date", "Issue Date", "date"),
            ("status", "Tax Status", "text"),
        ]

    elif document_type == "driving_license":
        fields = [
            ("license_number", "License Number", "text"),
            ("full_name", "Full Name", "text"),
            ("date_of_birth", "Date of Birth", "date"),
            ("permanent_address", "Permanent Address", "text"),
            ("license_category", "License Category (A/B/C/etc)", "text"),
            ("issue_date", "Issue Date", "date"),
            ("expiry_date", "Expiry Date", "date"),
            ("blood_group", "Blood Group", "text"),
        ]

    # ── Nepali Government Documents ──
    elif document_type == "land_ownership":
        fields = [
            ("lalmohan_number", "Lalmohan Number", "text"),
            ("owner_name", "Owner Name", "text"),
            ("father_name", "Father's Name", "text"),
            ("district", "District", "text"),
            ("vdc_municipality", "VDC / Municipality", "text"),
            ("ward_number", "Ward Number", "text"),
            ("kitta_number", "Kitta Number", "text"),
            ("area", "Area (Ropani/Bigha)", "text"),
            ("land_type", "Land Type", "text"),
            ("issue_date", "Issue Date", "date"),
        ]

    elif document_type == "vehicle_registration":
        fields = [
            ("registration_number", "Vehicle Registration Number", "text"),
            ("owner_name", "Owner Name", "text"),
            ("father_name", "Father's Name", "text"),
            ("permanent_address", "Permanent Address", "text"),
            ("vehicle_type", "Vehicle Type", "text"),
            ("make_model", "Make / Model", "text"),
            ("year_of_manufacture", "Year of Manufacture", "text"),
            ("chassis_number", "Chassis Number", "text"),
            ("engine_number", "Engine Number", "text"),
            ("registration_date", "Registration Date", "date"),
            ("validity_date", "Validity Date", "date"),
        ]

    elif document_type == "birth_certificate":
        fields = [
            ("child_name", "Child's Name", "text"),
            ("date_of_birth", "Date of Birth", "date"),
            ("place_of_birth", "Place of Birth", "text"),
            ("father_name", "Father's Name", "text"),
            ("mother_name", "Mother's Name", "text"),
            ("grandfather_name", "Grandfather's Name", "text"),
            ("permanent_address", "Permanent Address", "text"),
            ("gender", "Gender", "text"),
            ("registration_number", "Registration Number", "text"),
            ("issue_date", "Issue Date", "date"),
        ]

    elif document_type == "marriage_certificate":
        fields = [
            ("husband_name", "Husband's Name", "text"),
            ("wife_name", "Wife's Name", "text"),
            ("husband_father", "Husband's Father Name", "text"),
            ("wife_father", "Wife's Father Name", "text"),
            ("marriage_date", "Marriage Date", "date"),
            ("marriage_place", "Marriage Place", "text"),
            ("registration_number", "Registration Number", "text"),
            ("registered_at", "Registered At (Office)", "text"),
        ]

    elif document_type == "tax_certificate":
        fields = [
            ("taxpayer_name", "Taxpayer Name", "text"),
            ("pan_number", "PAN Number", "text"),
            ("fiscal_year", "Fiscal Year", "text"),
            ("tax_amount", "Tax Amount", "number"),
            ("issue_date", "Issue Date", "date"),
            ("certificate_type", "Certificate Type", "text"),
            ("remarks", "Remarks", "text"),
        ]

    elif document_type == "permit_license":
        fields = [
            ("permit_number", "Permit / License Number", "text"),
            ("holder_name", "Holder Name", "text"),
            ("permit_type", "Permit Type", "text"),
            ("issue_date", "Issue Date", "date"),
            ("expiry_date", "Expiry Date", "date"),
            ("issuing_authority", "Issuing Authority", "text"),
        ]

    # ── Bank Documents ──
    elif document_type == "bank_statement":
        fields = [
            ("account_number", "Account Number", "text"),
            ("account_holder", "Account Holder", "text"),
            ("bank_name", "Bank Name", "text"),
            ("branch_name", "Branch Name", "text"),
            ("statement_period", "Statement Period", "text"),
            ("opening_balance", "Opening Balance", "number"),
            ("closing_balance", "Closing Balance", "number"),
            ("total_credits", "Total Credits", "number"),
            ("total_debits", "Total Debits", "number"),
        ]

    elif document_type == "cheque":
        fields = [
            ("cheque_number", "Cheque Number", "text"),
            ("payee_name", "Payee Name", "text"),
            ("amount", "Amount (in figures)", "number"),
            ("amount_words", "Amount (in words)", "text"),
            ("cheque_date", "Cheque Date", "date"),
            ("drawer_account", "Drawer Account", "text"),
            ("drawee_bank", "Drawee Bank", "text"),
            ("branch", "Branch", "text"),
        ]

    elif document_type == "deposit_slip":
        fields = [
            ("account_number", "Account Number", "text"),
            ("account_holder", "Account Holder", "text"),
            ("deposit_date", "Deposit Date", "date"),
            ("deposit_amount", "Deposit Amount", "number"),
            ("deposit_type", "Deposit Type (Cash/Cheque)", "text"),
            ("cheque_number", "Cheque Number (if any)", "text"),
            ("bank_name", "Bank Name", "text"),
            ("branch_name", "Branch Name", "text"),
        ]

    elif document_type == "loan_agreement":
        fields = [
            ("loan_number", "Loan Account Number", "text"),
            ("borrower_name", "Borrower Name", "text"),
            ("loan_type", "Loan Type", "text"),
            ("loan_amount", "Loan Amount", "number"),
            ("interest_rate", "Interest Rate", "text"),
            ("loan_date", "Loan Date", "date"),
            ("maturity_date", "Maturity Date", "date"),
            ("emi_amount", "EMI Amount", "number"),
            ("collateral", "Collateral Details", "text"),
        ]

    elif document_type == "credit_card_statement":
        fields = [
            ("card_number", "Card Number (masked)", "text"),
            ("cardholder_name", "Cardholder Name", "text"),
            ("statement_date", "Statement Date", "date"),
            ("payment_due_date", "Payment Due Date", "date"),
            ("total_balance", "Total Balance", "number"),
            ("minimum_due", "Minimum Due", "number"),
            ("credit_limit", "Credit Limit", "number"),
            ("total_charges", "Total Charges", "number"),
            ("total_payments", "Total Payments", "number"),
        ]

    elif document_type == "bank_certificate":
        fields = [
            ("certificate_number", "Certificate Number", "text"),
            ("account_holder", "Account Holder", "text"),
            ("account_number", "Account Number", "text"),
            ("bank_name", "Bank Name", "text"),
            ("branch_name", "Branch Name", "text"),
            ("balance_amount", "Balance Amount", "number"),
            ("balance_as_of", "Balance As Of", "date"),
            ("issue_date", "Issue Date", "date"),
        ]

    elif document_type == "transaction_receipt":
        fields = [
            ("transaction_id", "Transaction ID", "text"),
            ("sender_name", "Sender Name", "text"),
            ("receiver_name", "Receiver Name", "text"),
            ("transaction_date", "Transaction Date", "date"),
            ("amount", "Transaction Amount", "number"),
            ("transaction_type", "Transaction Type", "text"),
            ("sender_account", "Sender Account", "text"),
            ("receiver_account", "Receiver Account", "text"),
            ("bank_name", "Bank Name", "text"),
            ("remarks", "Remarks", "text"),
        ]

    # ── Institute / Educational Documents ──
    elif document_type == "transcript":
        fields = [
            ("student_name", "Student Name", "text"),
            ("institution_name", "Institution Name", "text"),
            ("program", "Program / Major", "text"),
            ("semester_year", "Semester / Year", "text"),
            ("sgpa", "SGPA / GPA", "text"),
            ("cgpa", "CGPA", "text"),
            ("total_marks", "Total Marks", "text"),
            ("result_status", "Result Status", "text"),
            ("issue_date", "Issue Date", "date"),
        ]

    elif document_type == "certificate":
        fields = [
            ("student_name", "Student Name", "text"),
            ("institution_name", "Institution Name", "text"),
            ("certificate_type", "Certificate Type", "text"),
            ("completion_date", "Completion Date", "date"),
            ("registration_number", "Registration Number", "text"),
            ("grade_division", "Grade / Division", "text"),
            ("issue_date", "Issue Date", "date"),
        ]

    elif document_type == "admit_card":
        fields = [
            ("student_name", "Student Name", "text"),
            ("exam_name", "Exam Name", "text"),
            ("roll_number", "Roll Number", "text"),
            ("exam_date", "Exam Date", "date"),
            ("exam_center", "Exam Center", "text"),
            ("institution_name", "Institution Name", "text"),
            ("subjects", "Subjects", "text"),
        ]

    elif document_type == "degree":
        fields = [
            ("student_name", "Student Name", "text"),
            ("degree_title", "Degree Title", "text"),
            ("institution_name", "Institution Name", "text"),
            ("conferral_date", "Conferral Date", "date"),
            ("field_of_study", "Field of Study", "text"),
            ("honors", "Honors / Distinction", "text"),
        ]

    elif document_type == "character_certificate":
        fields = [
            ("student_name", "Student Name", "text"),
            ("institution_name", "Institution Name", "text"),
            ("character_rating", "Character Rating", "text"),
            ("issue_date", "Issue Date", "date"),
            ("purpose", "Purpose", "text"),
        ]

    elif document_type == "recommendation_letter":
        fields = [
            ("candidate_name", "Candidate Name", "text"),
            ("recommender_name", "Recommender Name", "text"),
            ("recommender_title", "Recommender Title", "text"),
            ("institution_name", "Institution Name", "text"),
            ("letter_date", "Letter Date", "date"),
            ("relationship", "Relationship to Candidate", "text"),
        ]

    elif document_type == "id_card":
        fields = [
            ("id_number", "ID Number", "text"),
            ("full_name", "Full Name", "text"),
            ("institution_name", "Institution / Organization", "text"),
            ("id_type", "ID Type (Student/Employee)", "text"),
            ("issue_date", "Issue Date", "date"),
            ("expiry_date", "Expiry Date", "date"),
        ]

    elif document_type == "registration_certificate":
        fields = [
            ("registration_number", "Registration Number", "text"),
            ("student_name", "Student Name", "text"),
            ("institution_name", "Institution Name", "text"),
            ("program", "Program", "text"),
            ("registration_date", "Registration Date", "date"),
            ("academic_year", "Academic Year", "text"),
        ]

    # ── Generic KYC / Identity ──
    elif document_type == "kyc_id":
        fields = [
            ("document_number", "Document ID Number", "text"),
            ("full_name", "Full Name", "text"),
            ("date_of_birth", "Date of Birth", "date"),
            ("nationality", "Nationality", "text"),
            ("expiry_date", "Expiry Date", "date"),
        ]

    # ── Medical ──
    elif document_type == "medical":
        fields = [
            ("patient_name", "Patient Name", "text"),
            ("doctor_name", "Doctor Name", "text"),
            ("date", "Date", "date"),
            ("diagnosis", "Diagnosis", "text"),
            ("prescription", "Prescription", "text"),
            ("hospital_name", "Hospital/Clinic Name", "text"),
        ]

    # ── Fallback: Generic ──
    else:
        fields = [
            ("document_title", "Document Title", "text"),
            ("document_date", "Document Date", "date"),
            ("extracted_summary", "Summary Notes", "text"),
        ]

    for name, label, dtype in fields:
        val_rules = rules if name == "total_amount" else {}
        ExtractionField.objects.create(
            workflow=workflow,
            name=name,
            label=label,
            data_type=dtype,
            validation_rules=val_rules
        )

    return workflow


def run_gemini_extraction(api_key, document, pages_images, fields_to_extract):
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)

        fields_schema = {f.name: {"type": f.data_type, "label": f.label, "required": f.is_required} for f in fields_to_extract}

        system_prompt = (
            "You are an enterprise AI document processing system. Analyze the attached document pages carefully.\n"
            "Identify all requested fields accurately including names, dates, amounts, checkboxes, tables, stamps, signature indicators, and cursive handwriting.\n"
            f"Document Type expected: {document.workflow.document_type}.\n"
            f"Fields to extract:\n{json.dumps(fields_schema, indent=2)}\n\n"
            "Return a structured JSON object containing:\n"
            "1. 'document_type': detected document type (e.g. invoice, kyc_id)\n"
            "2. 'fields': key-value pairs matching the exact field keys\n"
            "3. 'confidence_scores': key-value pairs of confidence score for each field (value from 0.0 to 1.0)\n"
            "4. 'bounding_boxes': optional bounding box for each field relative to page dimensions if visible, e.g. {'invoice_number': {'page': 1, 'x0': 150, 'y0': 100, 'x1': 300, 'y1': 150}}\n"
            "If a field value cannot be found in the document, set its value to null and confidence to 0.0. Do NOT fabricate data.\n"
            "Return raw JSON format ONLY."
        )

        if document.workflow.system_prompt_override:
            system_prompt += f"\nCustom Workspace Instructions: {document.workflow.system_prompt_override}"

        contents = [system_prompt]
        for page in pages_images:
            img = Image.open(page.image.path)
            contents.append(img)

        model = genai.GenerativeModel("models/gemini-2.5-flash")
        response = model.generate_content(contents)

        text = response.text
        text = re.sub(r"^```json\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```$", "", text, flags=re.MULTILINE)

        result = json.loads(text.strip())

        if "fields" not in result:
            result = {"fields": result}

        conf_scores = result.get("confidence_scores", {})
        if conf_scores:
            result["confidence_score"] = sum(conf_scores.values()) / len(conf_scores)
        else:
            result["confidence_score"] = 0.95

        return result
    except Exception as e:
        logger.error(f"Gemini API Extraction failed: {e}. Falling back to Local AI Simulator.")
        return None


def run_local_simulator_extraction(document, pages_images, fields_to_extract):
    extracted_text = document.raw_text
    doc_type = document.workflow.document_type

    fields = {}
    confidence_scores = {}
    bounding_boxes = {}

    lines = [l.strip() for l in extracted_text.split("\n") if l.strip()]

    for field in fields_to_extract:
        key = field.name
        val = None
        conf = 0.95

        val, conf = _extract_field_value(key, extracted_text, lines, doc_type, document)

        if val is not None:
            fields[key] = val
            confidence_scores[key] = conf
        else:
            fields[key] = None
            confidence_scores[key] = 0.0

        bounding_boxes[key] = {
            "page": 1,
            "x0": 120 + (document.id * 10) % 300,
            "y0": 200 + (len(fields) * 60) % 500,
            "x1": 350 + (document.id * 10) % 300,
            "y1": 240 + (len(fields) * 60) % 500
        }

    extracted_count = sum(1 for v in fields.values() if v is not None)
    total_count = len(fields)
    global_conf = (extracted_count / total_count * 0.9) if total_count > 0 else 0.5

    return {
        "document_type": doc_type,
        "fields": fields,
        "confidence_scores": confidence_scores,
        "bounding_boxes": bounding_boxes,
        "confidence_score": global_conf
    }


def _extract_field_value(key, extracted_text, lines, doc_type, document):
    val = None
    conf = 0.95

    if key in ("invoice_number", "invoice_no", "bill_number", "receipt_number", "document_number", "id_number"):
        patterns = [
            r"(?:invoice\s*(?:no|number|#|\.)\s*[:#]?\s*)([A-Za-z0-9][A-Za-z0-9/\-]{2,30})",
            r"(?:inv\s*(?:no|number|#|\.)\s*[:#]?\s*)([A-Za-z0-9][A-Za-z0-9/\-]{2,30})",
            r"(?:bill\s*(?:no|number|#|\.)\s*[:#]?\s*)([A-Za-z0-9][A-Za-z0-9/\-]{2,30})",
            r"(?:receipt\s*(?:no|number|#|\.)\s*[:#]?\s*)([A-Za-z0-9][A-Za-z0-9/\-]{2,30})",
            r"(?:order\s*(?:no|number|#|\.)\s*[:#]?\s*)([A-Za-z0-9][A-Za-z0-9/\-]{2,30})",
            r"(?:facture\s*(?:no|number|#|\.|:)\s*)([A-Za-z0-9][A-Za-z0-9/\-]{2,30})",
            r"(?:rechnung\s*(?:no|number|#|\.|:)\s*)([A-Za-z0-9][A-Za-z0-9/\-]{2,30})",
            r"\b(INV-\d{3,})\b",
            r"\b(\d{4,9})\s*$",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                break
        # fallback: look for alphanumeric code on first meaningful line
        if not val and lines:
            for line in lines[:5]:
                m = re.search(r'\b([A-Z0-9]{3,12}[-/]?\d{3,8})\b', line)
                if m:
                    val = m.group(1)

    elif key in ("vendor_name", "supplier_name", "seller_name", "company_name", "customer_name", "client_name", "bill_to", "name"):
        patterns = [
            r"(?:vendor|supplier|seller|company|from|bill\s*from|sold\s*by)\s*[:#]?\s*(.+)",
            r"(?:bill\s*to|ship\s*to|customer|client)\s*[:#]?\s*(.+)",
            r"(?:name|business|organization|firm)\s*[:#]?\s*(.+)",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip().rstrip(",")
                if len(candidate) > 2 and not re.match(r"^\d", candidate):
                    val = candidate
                    break
        if not val and lines:
            for line in lines[:3]:
                if len(line) > 5 and not re.match(r"^[\d\s\/\-]+$", line) and "invoice" not in line.lower() and "receipt" not in line.lower():
                    val = line
                    break

    elif key in ("invoice_date", "date", "document_date", "bill_date", "receipt_date", "issue_date"):
        patterns = [
            r"(?:invoice\s*date|date\s*of\s*issue|issue\s*date|bill\s*date|receipt\s*date|document\s*date)\s*[:#]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"(?:invoice\s*date|date\s*of\s*issue|issue\s*date|bill\s*date|receipt\s*date|document\s*date)\s*[:#]?\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})",
            r"(?:invoice\s*date|date\s*of\s*issue|issue\s*date|bill\s*date|receipt\s*date|document\s*date)\s*[:#]?\s*((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2},?\s*\d{4})",
            r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b",
            r"\b(\d{4}[/-]\d{1,2}[/-]\d{1,2})\b",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                conf = 0.9
                break

    elif key in ("due_date", "payment_due", "due_by", "expiry_date", "expiration_date"):
        patterns = [
            r"(?:due\s*date|payment\s*due|due\s*by|expiry\s*date|expiration\s*date|valid\s*until|expires)\s*[:#]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"(?:due\s*date|payment\s*due|due\s*by|expiry\s*date|expiration\s*date|valid\s*until|expires)\s*[:#]?\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})",
            r"(?:due\s*date|payment\s*due|due\s*by|expiry\s*date|expiration\s*date|valid\s*until|expires)\s*[:#]?\s*((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2},?\s*\d{4})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                conf = 0.9
                break

    elif key in ("subtotal", "sub_total", "net_amount", "subtotal_amount"):
        patterns = [
            r"(?:subtotal|sub\s*total|net\s*amount|sub\s*-\s*total|sub_total)\s*[:#]?\s*\$?\s*([\d,]+\.\d{2})",
            r"(?:subtotal|sub\s*total|net\s*amount|sub\s*-\s*total|sub_total)\s*[:#]?\s*(?:USD|EUR|GBP|INR|EUR|CAD|AUD)?\s*\$?\s*([\d,]+\.\d{2})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                conf = 0.92
                break

    elif key in ("tax_amount", "tax", "vat", "gst", "sales_tax", "vat_amount", "gst_amount"):
        patterns = [
            r"(?:tax|vat|gst|sales\s*tax|vat\s*amount|gst\s*amount|tax\s*amount)\s*[:#]?\s*\$?\s*([\d,]+\.\d{2})",
            r"(?:tax|vat|gst|sales\s*tax)\s*(?:\([^)]*\))?\s*[:#]?\s*\$?\s*([\d,]+\.\d{2})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                conf = 0.92
                break

    elif key in ("total_amount", "total", "grand_total", "amount_due", "total_due", "balance_due", "amount_payable", "total_incl", "total_invoice"):
        patterns = [
            r"(?:total|grand\s*total|amount\s*due|total\s*due|balance\s*due|total\s*amount|amount\s*payable|total\s*incl|total\s*invoice)\s*[:#]?\s*\$?\s*([\d,]+\.\d{2})",
            r"(?:total|amount)\s*(?:due|payable)?\s*(?:USD|EUR|GBP|INR|CAD|AUD)?\s*\$?\s*([\d,]+\.\d{2})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                conf = 0.92
                break
        if not val:
            numeric_vals = re.findall(r"\b(\d+\.\d{2})\b", extracted_text)
            if numeric_vals:
                val = max(float(v.replace(",", "")) for v in numeric_vals)
                val = f"{val:.2f}"
                conf = 0.8

    elif key in ("full_name", "holder_name", "cardholder_name", "customer_name", "client_name"):
        patterns = [
            r"(?:name|full\s*name|holder|cardholder|customer|client|passenger|applicant)\s*[:#]?\s*([A-Za-z\s'-]{3,50})",
            r"(?:mr|mrs|ms|dr|prof)\.?\s*([A-Za-z\s'-]{3,50})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip().rstrip(",")
                if len(candidate) > 3:
                    val = candidate
                    break

    elif key in ("date_of_birth", "dob", "birth_date"):
        patterns = [
            r"(?:date\s*of\s*birth|dob|birth\s*date|born|birthday)\s*[:#]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            r"(?:date\s*of\s*birth|dob|birth\s*date|born|birthday)\s*[:#]?\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                conf = 0.9
                break

    elif key in ("nationality", "citizenship", "country"):
        patterns = [
            r"(?:nationality|citizenship|country|nation)\s*[:#]?\s*([A-Za-z\s]{3,30})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip().rstrip(",").strip()
                if len(val) > 2:
                    break

    elif key in ("document_title", "title", "subject", "description"):
        if lines:
            for line in lines[:3]:
                if len(line) > 10 and not re.match(r"^[\d\s\/\-:.]+$", line):
                    val = line
                    conf = 0.85
                    break

    elif key in ("extracted_summary", "notes", "summary", "remarks"):
        if lines and len(lines) > 3:
            val = " ".join(lines[3:8])
            conf = 0.7

    elif key in ("po_number", "purchase_order", "order_number", "reference"):
        patterns = [
            r"(?:po\s*(?:number|#|no)|purchase\s*order|order\s*(?:number|#|no)|ref(?:erence)?)\s*[:#]?\s*([A-Za-z0-9][A-Za-z0-9/\-]{2,20})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                break

    elif key in ("currency", "currency_code"):
        m = re.search(r"\b(USD|EUR|GBP|INR|JPY|CNY|CAD|AUD|CHF|HKD|SGD|NZD|KRW|SEK|NOK|DKK)\b", extracted_text)
        if m:
            val = m.group(1)
            conf = 0.95

    elif key in ("payment_method", "payment_terms", "payment_mode"):
        patterns = [
            r"(?:payment\s*(?:method|terms|mode|type))\s*[:#]?\s*(.+)",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip().rstrip(",").strip()
                if len(val) > 1:
                    break

    elif key in ("iban", "account_number", "bank_account", "sort_code", "routing"):
        patterns = [
            r"(?:iban|account\s*(?:number|no|#)|bank\s*account|sort\s*code|routing)\s*[:#]?\s*([A-Za-z0-9]{5,30})",
        ]
        for p in patterns:
            m = re.search(p, extracted_text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                break

    return val, conf


def evaluate_validation_rules(document, fields_to_extract, extracted_fields):
    validation_passed = True
    doc_validation_status = Document.VALIDATION_VERIFIED

    DocumentFieldResult.objects.filter(document=document).delete()

    for field in fields_to_extract:
        key = field.name
        val = extracted_fields.get(key)

        field_status = "verified"
        msg = ""

        if field.is_required and (val is None or str(val).strip() == ""):
            field_status = "failed"
            msg = "Field is required but was not extracted."
            validation_passed = False
            doc_validation_status = Document.VALIDATION_FAILED

        elif val and "regex" in field.validation_rules:
            pattern = field.validation_rules["regex"]
            if not re.match(pattern, str(val)):
                field_status = "warning"
                msg = f"Value '{val}' does not match validation format pattern."
                doc_validation_status = Document.VALIDATION_WARNING

        elif val and "math_expression" in field.validation_rules:
            expr = field.validation_rules["math_expression"]
            try:
                vars_in_expr = re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", expr)
                eval_expr = expr

                for v in vars_in_expr:
                    if v in extracted_fields:
                        v_val = clean_number(extracted_fields[v])
                        if v_val is None:
                            v_val = 0.0
                        eval_expr = re.sub(r"\b" + re.escape(v) + r"\b", str(v_val), eval_expr)

                safe_expr = eval_expr.replace("==", "==").replace("+", "+").replace("-", "-").replace("*", "*").replace("/", "/")

                if not re.match(r"^[0-9\s\.\+\-\*\/\=\!\<\>\(\)]*$", safe_expr):
                    raise ValueError("Unsafe tokens in math validation expression.")

                if "==" in safe_expr:
                    left, right = safe_expr.split("==", 1)
                    left_val = eval(left.strip(), {"__builtins__": {}}, {})
                    right_val = eval(right.strip(), {"__builtins__": {}}, {})
                    is_valid = abs(float(left_val) - float(right_val)) < 0.05
                else:
                    is_valid = bool(eval(safe_expr, {"__builtins__": {}}, {}))

                if not is_valid:
                    field_status = "failed"
                    msg = f"Mathematical validation failed: {expr} (calculated: {eval_expr})"
                    validation_passed = False
                    doc_validation_status = Document.VALIDATION_FAILED
            except Exception as e:
                field_status = "warning"
                msg = f"Math rules evaluation skipped: {str(e)}"
                doc_validation_status = Document.VALIDATION_WARNING

        bboxes = document.extracted_json.get("bounding_boxes") or {}
        bbox = bboxes.get(key) or {}

        DocumentFieldResult.objects.create(
            document=document,
            field_name=key,
            field_label=field.label,
            extracted_value=val,
            confidence=document.extracted_json.get("confidence_scores", {}).get(key, 0.9),
            validation_status=field_status,
            validation_message=msg,
            bounding_box=bbox
        )

    document.validation_status = doc_validation_status
    document.save(update_fields=["validation_status"])
    return validation_passed


def clean_number(val):
    if not val:
        return None
    cleaned = re.sub(r"[^\d\.-]", "", str(val))
    try:
        return float(cleaned)
    except ValueError:
        return None


def trigger_websocket_update(document):
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync

        channel_layer = get_channel_layer()
        if channel_layer:
            group_name = f"workspace_{document.workspace.id}"
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "document_update",
                    "data": {
                        "event": "document.processed",
                        "document_id": document.id,
                        "name": document.name,
                        "status": document.status,
                        "validation_status": document.validation_status,
                        "fraud_status": document.fraud_status,
                        "confidence_score": document.confidence_score
                    }
                }
            )
    except Exception as e:
        logger.error(f"WebSocket notification broadcast failed: {e}")


def trigger_webhooks(document):
    try:
        from api.tasks import dispatch_webhook_notifications
        dispatch_webhook_notifications(document.id)
    except Exception as e:
        logger.error(f"Failed to dispatch webhooks: {e}")
