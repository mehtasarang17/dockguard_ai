"""
Document processing logic — extracted from app.py for reuse by both
Celery tasks and fallback threading.

This module contains the core background processing functions that:
1. Extract text from uploaded files
2. Run the analysis pipeline via the Orchestrator
3. Save results to the database

Each function receives a db_name parameter to connect to the correct
tenant database (database-per-tenant isolation).
"""

import time
import traceback

from models import get_central_db, Document, Analysis, BatchAnalysis, SystemSettings
from tenant_db import get_tenant_session
from extractor import extract_text
from agents.orchestrator import Orchestrator
import framework_store
from sqlalchemy.orm.attributes import flag_modified


def _increment_lifetime_tokens(token_count: int):
    """Atomically add token_count to the lifetime_tokens SystemSettings record.

    Uses the central DB since SystemSettings is a global/admin table.
    """
    if not token_count:
        return
    db = get_central_db()
    try:
        setting = db.query(SystemSettings).filter(SystemSettings.key == 'lifetime_tokens').first()
        if setting:
            setting.value = str(int(setting.value) + token_count)
        else:
            setting = SystemSettings(key='lifetime_tokens', value=str(token_count))
            db.add(setting)
        db.commit()
    except Exception as e:
        print(f"⚠️  Failed to update lifetime_tokens: {e}")
    finally:
        db.close()


def process_document(db_name: str, document_id: int, file_path: str, document_type: str):
    """Analyse a single document end-to-end (text extraction → pipeline → DB save)."""
    db = get_tenant_session(db_name)
    try:
        doc = db.query(Document).filter(Document.id == document_id).first()
        doc.status = 'processing'
        db.commit()

        text = extract_text(file_path)

        # Cache the extracted Markdown so future saves skip re-parsing the file
        doc.markdown_text = text
        db.commit()

        start = time.time()

        # Skip framework analysis during initial processing.
        # User selects frameworks to check after analysis completes.
        skip_all = {k: False for k in framework_store.FRAMEWORK_KEYS}

        orchestrator = Orchestrator()
        result = orchestrator.run(document_id, text, document_type,
                                  uploaded_frameworks=skip_all)
        elapsed = time.time() - start

        scoring = result.get('scoring_details', {})

        analysis = Analysis(
            document_id=document_id,
            compliance_score=result.get('compliance_score', 0),
            security_score=result.get('security_score', 0),
            risk_score=result.get('risk_score', 0),
            overall_score=result.get('overall_score', 0),
            completeness_score=scoring.get('completeness', {}).get('score', 0),
            security_strength_score=scoring.get('security_strength', {}).get('score', 0),
            coverage_score=scoring.get('coverage', {}).get('score', 0),
            clarity_score=scoring.get('clarity', {}).get('score', 0),
            enforcement_score=scoring.get('enforcement_level', {}).get('score', 0),
            compliance_findings=result.get('compliance_findings', []),
            security_findings=result.get('security_findings', []),
            risk_findings=result.get('risk_findings', []),
            framework_mappings=result.get('framework_mappings', {}),
            gap_detections=result.get('gap_detections', []),
            best_practices=result.get('best_practices', []),
            suggestions=result.get('auto_suggestions', []),
            risk_level=result.get('risk_level', 'medium'),
            document_maturity=result.get('document_maturity', 'basic'),
            recommendations=result.get('recommendations', []),
            score_rationale=result.get('score_rationale', []),
            input_tokens=result.get('input_tokens', 0),
            output_tokens=result.get('output_tokens', 0),
            total_tokens=result.get('total_tokens', 0),
            processing_time=round(elapsed, 2),
        )
        db.add(analysis)
        doc.status = 'completed'
        db.commit()
        _increment_lifetime_tokens(result.get('total_tokens', 0))
        print(f"✅ Document {document_id} analysed in {elapsed:.1f}s")

    except Exception as e:
        print(f"❌ Error processing document {document_id}: {e}")
        traceback.print_exc()
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc:
            doc.status = 'failed'
            db.commit()
    finally:
        db.close()


def process_batch(db_name: str, batch_id: int, documents_info: list, document_type: str):
    """Analyse multiple documents and run cross-doc synthesis."""
    db = get_tenant_session(db_name)
    try:
        batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id).first()
        if not batch:
            return

        # Extract text for each document
        documents = []
        for info in documents_info:
            doc = db.query(Document).filter(Document.id == info['id']).first()
            if doc:
                doc.status = 'processing'
                db.commit()
                try:
                    text = extract_text(info['file_path'])
                    # Cache the extracted Markdown
                    doc.markdown_text = text
                    db.commit()

                    text_len = len(text.strip()) if text else 0
                    print(f"📄 Extracted text for '{info['filename']}': {text_len} chars")
                    if text_len == 0:
                        print(f"⚠️  Empty text for '{info['filename']}' — marking as failed")
                        doc.status = 'failed'
                        db.commit()
                        continue
                    documents.append({
                        'id': info['id'],
                        'filename': info['filename'],
                        'text': text,
                    })
                except Exception as e:
                    print(f"❌ Error extracting text for {info['filename']}: {e}")
                    doc.status = 'failed'
                    db.commit()

        if not documents:
            batch.status = 'failed'
            db.commit()
            return

        # Run batch analysis with a fresh Orchestrator
        batch_orchestrator = Orchestrator()
        result = batch_orchestrator.run_batch(documents, document_type)

        # Save individual analyses
        for ir in result.get('individual_results', []):
            doc_id = ir['document_id']
            r = ir['result']
            doc = db.query(Document).filter(Document.id == doc_id).first()
            if not doc:
                continue

            scoring = r.get('scoring_details', {})

            analysis = Analysis(
                document_id=doc_id,
                compliance_score=r.get('compliance_score', 0),
                security_score=r.get('security_score', 0),
                risk_score=r.get('risk_score', 0),
                overall_score=r.get('overall_score', 0),
                completeness_score=scoring.get('completeness', {}).get('score', 0),
                security_strength_score=scoring.get('security_strength', {}).get('score', 0),
                coverage_score=scoring.get('coverage', {}).get('score', 0),
                clarity_score=scoring.get('clarity', {}).get('score', 0),
                enforcement_score=scoring.get('enforcement_level', {}).get('score', 0),
                compliance_findings=r.get('compliance_findings', []),
                security_findings=r.get('security_findings', []),
                risk_findings=r.get('risk_findings', []),
                framework_mappings=r.get('framework_mappings', {}),
                gap_detections=r.get('gap_detections', []),
                best_practices=r.get('best_practices', []),
                suggestions=r.get('auto_suggestions', []),
                risk_level=r.get('risk_level', 'medium'),
                document_maturity=r.get('document_maturity', 'basic'),
                recommendations=r.get('recommendations', []),
                score_rationale=r.get('score_rationale', []),
                input_tokens=r.get('input_tokens', 0),
                output_tokens=r.get('output_tokens', 0),
                total_tokens=r.get('total_tokens', 0),
                processing_time=r.get('processing_time', 0),
            )
            db.add(analysis)
            doc.status = 'completed'

        # Save batch results
        synthesis = result.get('synthesis', {})
        batch.overall_score = synthesis.get('overall_score', 0)
        batch.risk_level = synthesis.get('risk_level', 'medium')
        batch.document_maturity = synthesis.get('document_maturity', 'developing')
        batch.cross_doc_gaps = result.get('cross_doc_gaps', {})
        batch.synthesis = synthesis

        # Capture the computed total tokens properly into the synthesis JSON
        batch_tokens = result.get('total_tokens', 0)
        batch.synthesis['total_tokens'] = batch_tokens
        flag_modified(batch, "synthesis")  # Tell SQLAlchemy the JSON changed

        batch.recommendations = synthesis.get('top_priorities', [])
        batch.score_rationale = synthesis.get('score_rationale', [])
        batch.processing_time = result.get('processing_time', 0)
        batch.status = 'completed'

        db.commit()

        # Increment global lifetime counter
        _increment_lifetime_tokens(batch_tokens)
        print(f"✅ Batch {batch_id} completed: {len(documents)} documents in {result.get('processing_time', 0):.1f}s")

    except Exception as e:
        print(f"❌ Batch {batch_id} failed: {e}")
        traceback.print_exc()
        try:
            batch = db.query(BatchAnalysis).filter(BatchAnalysis.id == batch_id).first()
            if batch:
                batch.status = 'failed'
                db.commit()
        except Exception:
            pass
    finally:
        db.close()
