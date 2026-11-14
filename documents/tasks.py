import threading
import logging
from celery import shared_task
from .ai_pipeline import process_document

logger = logging.getLogger(__name__)

@shared_task
def process_document_task(document_id):
    """
    Celery task representing async processing of a document page.
    Only runs if a Celery worker is actively consuming tasks.
    """
    logger.info(f"Running Celery task process_document_task for ID {document_id}")
    process_document(document_id)


def trigger_document_processing(document_id):
    """
    Orchestrates document processing asynchronously.
    Uses a background thread by default (always works, no Celery worker needed).
    Falls back to synchronous only during unit tests.
    """
    import sys
    if any("test" in arg for arg in sys.argv):
        process_document(document_id)
        return

    thread = threading.Thread(target=process_document, args=(document_id,))
    thread.daemon = True
    thread.start()
    logger.info(f"Background thread started for document {document_id}.")
