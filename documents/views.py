import os
import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseForbidden, HttpResponse
from django.core.files.storage import FileSystemStorage
from django.contrib import messages
from django.utils import timezone
from core.models import User
from .models import Workflow, ExtractionField, Document, DocumentPage, DocumentFieldResult
from .tasks import trigger_document_processing
from .ai_pipeline import evaluate_validation_rules
from .ocr import extract_text_multi_format
import docx
import io

@login_required
def workflow_list(request):
    """Lists all active custom workflows inside the workspace."""
    workspace = request.workspace
    if not workspace:
        return redirect("core:create_workspace")

    workflows = Workflow.objects.filter(workspace=workspace).prefetch_related("fields")
    return render(request, "documents/workflow_list.html", {"workflows": workflows})


@login_required
def workflow_create(request):
    """Allows admins to create new workflows."""
    return _workflow_upsert(request, None)


@login_required
def workflow_edit(request, workflow_id):
    """Allows admins to edit existing workflows."""
    workflow = get_object_or_404(Workflow, id=workflow_id, workspace=request.workspace)
    return _workflow_upsert(request, workflow)


def _workflow_upsert(request, workflow):
    """Shared create/edit handler for workflows."""
    workspace = request.workspace
    is_edit = workflow is not None

    if request.user.role != User.ROLE_ADMIN:
        messages.error(request, "Only administrators can configure workflows.")
        return redirect("documents:workflow_list")

    if request.method == "POST":
        name = request.POST.get("name")
        doc_type = request.POST.get("document_type")
        prompt = request.POST.get("system_prompt_override", "")

        field_keys = request.POST.getlist("field_key[]")
        field_labels = request.POST.getlist("field_label[]")
        field_types = request.POST.getlist("field_type[]")
        field_required = request.POST.getlist("field_required[]")

        if name and doc_type:
            if is_edit:
                workflow.name = name
                workflow.document_type = doc_type
                workflow.system_prompt_override = prompt
                workflow.save()
                workflow.fields.all().delete()
            else:
                workflow = Workflow.objects.create(
                    workspace=workspace,
                    name=name,
                    document_type=doc_type,
                    system_prompt_override=prompt
                )

            for i in range(len(field_keys)):
                key = field_keys[i].strip()
                label = field_labels[i].strip()
                dtype = field_types[i]
                req = str(i) in field_required or field_keys[i] in field_required

                if key and label:
                    rules = {}
                    if key == "total_amount":
                        rules = {"math_expression": "subtotal + tax_amount == total_amount"}
                    ExtractionField.objects.create(
                        workflow=workflow,
                        name=key,
                        label=label,
                        data_type=dtype,
                        is_required=req,
                        validation_rules=rules
                    )

            messages.success(request, f"Workflow '{name}' {'updated' if is_edit else 'created'} successfully!")
            return redirect("documents:workflow_list")
        else:
            messages.error(request, "Workflow name and document type are required.")

    editing_fields = []
    if workflow:
        editing_fields = [
            {"key": f.name, "label": f.label, "type": f.data_type, "req": f.is_required}
            for f in workflow.fields.all()
        ]
    context = {"editing": workflow, "editing_fields": json.dumps(editing_fields)}
    return render(request, "documents/workflow_create.html", context)


@login_required
def workflow_delete(request, workflow_id):
    """Deletes workflow templates."""
    workflow = get_object_or_404(Workflow, id=workflow_id, workspace=request.workspace)
    if request.user.role != User.ROLE_ADMIN:
        messages.error(request, "Admin permission required.")
        return redirect("documents:workflow_list")
        
    name = workflow.name
    workflow.delete()
    messages.success(request, f"Workflow '{name}' deleted.")
    return redirect("documents:workflow_list")


@login_required
@csrf_exempt
def document_upload(request):
    """
    Handles B2B async document upload requests.
    Accepts optional workflow_id to select which extraction workflow to use.
    Creates Document entry and starts async background pipeline (Celery or Thread fallback).
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    workspace = request.workspace
    if not workspace:
        return JsonResponse({"error": "Workspace not resolved"}, status=400)

    file_obj = request.FILES.get("file")
    if not file_obj:
        return JsonResponse({"error": "No file uploaded"}, status=400)

    filename = file_obj.name
    ext = os.path.splitext(filename)[1].replace(".", "").lower()
    
    # Validate extension
    allowed_exts = ["pdf", "png", "jpg", "jpeg", "tiff", "heic"]
    if ext not in allowed_exts:
        return JsonResponse({"error": f"Unsupported extension: {ext}"}, status=400)

    # Resolve optional workflow selection
    workflow_id = request.POST.get("workflow_id") or request.POST.get("workflow")
    workflow = None
    if workflow_id:
        try:
            workflow = Workflow.objects.get(id=workflow_id, workspace=workspace)
        except (Workflow.DoesNotExist, ValueError):
            return JsonResponse({"error": "Invalid workflow_id"}, status=400)

    # 1. Create Document model with chosen workflow
    document = Document.objects.create(
        workspace=workspace,
        workflow=workflow,
        name=filename,
        file=file_obj,
        file_type=ext,
        status=Document.STATUS_PENDING
    )

    # 2. Trigger asynchronous background process (using Celery/Thread fallback)
    trigger_document_processing(document.id)

    return JsonResponse({
        "message": "File enqueued for AI extraction",
        "document": {
            "id": document.id,
            "name": document.name,
            "file_type": document.file_type,
            "status": document.status,
            "workflow_id": workflow.id if workflow else None,
            "workflow_name": workflow.name if workflow else None,
            "created_at": document.created_at.strftime("%Y-%m-%d %H:%M")
        }
    }, status=201)


@login_required
def review_detail(request, document_id):
    """Renders the Side-by-side Human Review Dashboard verification panel."""
    document = get_object_or_404(Document, id=document_id, workspace=request.workspace)
    pages = document.pages.all()
    field_results = document.field_results.all()
    
    # Audit log entry for opening document
    from analytics.models import AuditLog
    AuditLog.objects.create(
        workspace=request.workspace,
        user=request.user,
        action="VIEW_DOCUMENT_REVIEW",
        details={"document_id": document.id, "name": document.name}
    )

    context = {
        "document": document,
        "pages": pages,
        "field_results": field_results,
    }
    return render(request, "review/dashboard.html", context)


@login_required
def review_submit(request, document_id):
    """
    Submits manual corrections from reviewer.
    Saves new values, runs re-validation rules, updates audit history, and completes document.
    """
    document = get_object_or_404(Document, id=document_id, workspace=request.workspace)
    if request.method != "POST":
        return redirect("documents:review_detail", document_id=document.id)

    # Resolve active workflow fields
    workflow = document.workflow
    if not workflow:
        messages.error(request, "No extraction workflow mapped to document.")
        return redirect("core:dashboard")

    fields = workflow.fields.all()
    
    # Prepare data dict of corrected inputs
    corrections = {}
    for field in fields:
        form_val = request.POST.get(field.name)
        # Update DocumentFieldResult
        result = DocumentFieldResult.objects.filter(document=document, field_name=field.name).first()
        if result:
            # Save correction
            result.corrected_value = form_val
            result.save()
            corrections[field.name] = form_val

    # Run mathematical and validation check rules again over corrected dataset
    evaluate_validation_rules(document, fields, corrections)

    # Save final reviewer context
    document.status = Document.STATUS_COMPLETED
    document.reviewer = request.user
    document.reviewed_at = timezone.now()
    document.save(update_fields=["status", "reviewer", "reviewed_at"])

    # Create Audit Log
    from analytics.models import AuditLog
    AuditLog.objects.create(
        workspace=request.workspace,
        user=request.user,
        action="SUBMIT_DOCUMENT_CORRECTION",
        details={"document_id": document.id, "name": document.name}
    )

    # Notify dashboard socket
    from .ai_pipeline import trigger_websocket_update
    trigger_websocket_update(document)

    messages.success(request, f"Document '{document.name}' verified and locked successfully!")
    return redirect("core:dashboard")


@login_required
def document_delete(request, document_id):
    """Deletes a document and its associated pages, field results, and fraud checks."""
    document = get_object_or_404(Document, id=document_id, workspace=request.workspace)

    name = document.name
    doc_id = document.id

    for page in document.pages.all():
        if page.image:
            page.image.delete(save=False)
    if document.file:
        document.file.delete(save=False)

    document.delete()

    from analytics.models import AuditLog
    AuditLog.objects.create(
        workspace=request.workspace,
        user=request.user,
        action="DELETE_DOCUMENT",
        details={"document_id": doc_id, "name": name}
    )

    trigger_websocket_update_removed(doc_id, request.workspace.id)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"message": f"Document '{name}' deleted."}, status=200)

    messages.success(request, f"Document '{name}' deleted.")
    return redirect("core:dashboard")


def trigger_websocket_update_removed(document_id, workspace_id):
    """Notify connected clients that a document was removed."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        channel_layer = get_channel_layer()
        if channel_layer:
            group_name = f"workspace_{workspace_id}"
            async_to_sync(channel_layer.group_send)(
                group_name,
                {
                    "type": "document_update",
                    "data": {
                        "event": "document.removed",
                        "document_id": document_id,
                    }
                }
            )
    except Exception as e:
        logger.error(f"WebSocket delete notification failed: {e}")

@login_required
def text_extract_view(request):
    """View to handle Nepali and English text extraction from uploaded images/charts."""
    if request.method == "POST" and request.FILES.get("document"):
        upload = request.FILES["document"]
        fs = FileSystemStorage(location=os.path.join("media", "temp_ocr"))
        filename = fs.save(upload.name, upload)
        file_path = fs.path(filename)
        
        # Run multi-format extraction
        extracted_text = extract_text_multi_format(file_path)
        
        # Clean up temp file
        if os.path.exists(file_path):
            os.remove(file_path)
            
        # Optional: Clean up txt file created by OCR
        txt_path = os.path.splitext(file_path)[0] + '.nep_eng.txt'
        if os.path.exists(txt_path):
            os.remove(txt_path)
        
        # Generate DOCX in memory
        doc = docx.Document()
        doc.add_heading('Extracted Text', 0)
        doc.add_paragraph(extracted_text)
        
        buffer = io.BytesIO()
        doc.save(buffer)
        buffer.seek(0)
        
        response = HttpResponse(buffer.getvalue(), content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        response['Content-Disposition'] = f'attachment; filename="extracted_text.docx"'
        return response

    return render(request, "documents/text_extract.html")
