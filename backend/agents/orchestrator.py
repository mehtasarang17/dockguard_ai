"""
LangGraph-based orchestration pipeline for document analysis.

Pipeline: synopsis → compliance → security → risk → framework → gap_detection → scoring → best_practices → suggestions → finalize
                                                                                            ↑ receives synopsis + upstream findings
Key features:
- Synopsis agent runs first, feeds context to all downstream agents
- Hybrid model routing: Haiku for structured extraction, Sonnet for reasoning
- Gap detection receives upstream findings to avoid duplicates
- Best practices receives gap detections to avoid repeats
- Suggestions agent receives ALL upstream findings
- Finalize uses LLM to synthesize deduplicated recommendations
"""

import json
import time
import traceback
from typing import TypedDict, List, Dict, Optional, Any

from langgraph.graph import StateGraph, END
from agents.bedrock_client import BedrockClient
from agents.llm_factory import get_llm_client
from agents.prompts import (
    synopsis_prompt,
    compliance_prompt,
    security_prompt,
    risk_prompt,
    framework_mapping_prompt,
    framework_comparison_prompt,
    single_framework_llm_prompt,
    gap_detection_prompt,
    scoring_prompt,
    best_practices_prompt,
    auto_suggest_prompt,
    recommendations_prompt,
    multi_doc_gap_prompt,
    multi_doc_synthesis_prompt,
)
import framework_store

# ---- State -------------------------------------------------------------------

class AnalysisState(TypedDict):
    document_id: int
    document_text: str
    document_type: str

    # Synopsis (shared by all agents)
    synopsis: Optional[Dict]

    # Agent outputs
    compliance_findings: List[Dict]
    compliance_score: int
    security_findings: List[Dict]
    security_score: int
    risk_findings: List[Dict]
    risk_score: int
    risk_level: str
    framework_mappings: Dict
    gap_detections: List[Dict]
    scoring_details: Dict
    best_practices: List[Dict]
    auto_suggestions: List[Dict]
    recommendations: List[Dict]
    overall_score: int
    score_rationale: List[str]
    document_maturity: str
    processing_time: float

    # Framework control
    uploaded_frameworks: Dict

    # Step tracking
    current_step: int
    errors: List[str]


# ---- Orchestrator class ------------------------------------------------------

class Orchestrator:
    def __init__(self):
        self.graph = self._build_graph()

    def run(self, document_id: int, text: str, doc_type: str,
            uploaded_frameworks: dict = None) -> dict:
        """Run the full analysis pipeline for a single document."""
        start = time.time()
        llm = get_llm_client()  # Factory: returns Bedrock or Ollama based on settings
        llm.reset_tokens()
        if uploaded_frameworks is None:
            uploaded_frameworks = {k: False for k in framework_store.FRAMEWORK_KEYS}

        initial_state: AnalysisState = {
            "document_id": document_id,
            "document_text": text,
            "document_type": doc_type,
            "synopsis": None,
            "compliance_findings": [],
            "compliance_score": 0,
            "security_findings": [],
            "security_score": 0,
            "risk_findings": [],
            "risk_score": 0,
            "risk_level": "unknown",
            "framework_mappings": {},
            "gap_detections": [],
            "scoring_details": {},
            "best_practices": [],
            "auto_suggestions": [],
            "recommendations": [],
            "overall_score": 0,
            "score_rationale": [],
            "document_maturity": "unknown",
            "processing_time": 0,
            "uploaded_frameworks": uploaded_frameworks,
            "current_step": 0,
            "errors": [],
        }

        result = self.graph.invoke(initial_state, config={"configurable": {"llm": llm}}) # Pass llm to graph
        result["processing_time"] = round(time.time() - start, 2)
        result["input_tokens"] = llm.total_input_tokens
        result["output_tokens"] = llm.total_output_tokens
        result["total_tokens"] = llm.total_input_tokens + llm.total_output_tokens
        return result

    # ---- Graph builder -------------------------------------------------------
    def _build_graph(self) -> StateGraph:
        g = StateGraph(AnalysisState)

        g.add_node("synopsis_agent", self._synopsis_agent)
        g.add_node("compliance_agent", self._compliance_agent)
        g.add_node("security_agent", self._security_agent)
        g.add_node("risk_agent", self._risk_agent)
        g.add_node("framework_agent", self._framework_agent)
        g.add_node("gap_detection_agent", self._gap_detection_agent)
        g.add_node("scoring_agent", self._scoring_agent)
        g.add_node("best_practices_agent", self._best_practices_agent)
        g.add_node("suggestion_agent", self._suggestion_agent)
        g.add_node("finalize", self._finalize)

        g.set_entry_point("synopsis_agent")
        g.add_edge("synopsis_agent", "compliance_agent")
        g.add_edge("compliance_agent", "security_agent")
        g.add_edge("security_agent", "risk_agent")
        g.add_edge("risk_agent", "framework_agent")
        g.add_edge("framework_agent", "gap_detection_agent")
        g.add_edge("gap_detection_agent", "scoring_agent")
        g.add_edge("scoring_agent", "best_practices_agent")
        g.add_edge("best_practices_agent", "suggestion_agent")
        g.add_edge("suggestion_agent", "finalize")
        g.add_edge("finalize", END)

        return g.compile()

    # ---- Agent implementations -----------------------------------------------

    def _synopsis_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 0: Extract document synopsis (fast model)."""
        llm = config["configurable"]["llm"]
        print("🔍 Agent 0/9: Synopsis — extracting document structure...")
        try:
            prompt = synopsis_prompt(state["document_text"], state["document_type"])
            raw = llm.invoke_fast(prompt, max_tokens=2048)
            data = llm.parse_json(raw)
            print(f"   ✅ Synopsis: {data.get('document_title', 'unknown')}")
            return {"synopsis": data, "current_step": 1}
        except Exception as e:
            print(f"   ⚠️ Synopsis failed: {e}")
            return {"synopsis": None, "current_step": 1, "errors": state["errors"] + [f"synopsis: {e}"]}

    def _compliance_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 1: Compliance analysis (fast model)."""
        llm = config["configurable"]["llm"]
        print("📋 Agent 1/9: Compliance Analysis...")
        try:
            prompt = compliance_prompt(
                state["document_text"], state["document_type"],
                synopsis=state.get("synopsis")
            )
            raw = llm.invoke_fast(prompt, max_tokens=4096)
            data = llm.parse_json(raw)
            findings = data.get("findings", [])
            score = data.get("score", 0)
            print(f"   ✅ {len(findings)} findings, score={score}")
            return {"compliance_findings": findings, "compliance_score": score, "current_step": 2}
        except Exception as e:
            print(f"   ⚠️ Compliance failed: {e}")
            traceback.print_exc()
            return {"current_step": 2, "errors": state["errors"] + [f"compliance: {e}"]}

    def _security_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 2: Security analysis (fast model)."""
        llm = config["configurable"]["llm"]
        print("🔒 Agent 2/9: Security Analysis...")
        try:
            prompt = security_prompt(
                state["document_text"], state["document_type"],
                synopsis=state.get("synopsis")
            )
            raw = llm.invoke_fast(prompt, max_tokens=4096)
            data = llm.parse_json(raw)
            findings = data.get("findings", [])
            score = data.get("score", 0)
            print(f"   ✅ {len(findings)} findings, score={score}")
            return {"security_findings": findings, "security_score": score, "current_step": 3}
        except Exception as e:
            print(f"   ⚠️ Security failed: {e}")
            traceback.print_exc()
            return {"current_step": 3, "errors": state["errors"] + [f"security: {e}"]}

    def _risk_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 3: Risk analysis (fast model)."""
        llm = config["configurable"]["llm"]
        print("⚠️ Agent 3/9: Risk Analysis...")
        try:
            prompt = risk_prompt(
                state["document_text"], state["document_type"],
                synopsis=state.get("synopsis")
            )
            raw = llm.invoke_fast(prompt, max_tokens=4096)
            data = llm.parse_json(raw)
            findings = data.get("findings", [])
            score = data.get("score", 0)
            risk_level = data.get("risk_level", "medium")
            print(f"   ✅ {len(findings)} risks, score={score}, level={risk_level}")
            return {
                "risk_findings": findings,
                "risk_score": score,
                "risk_level": risk_level,
                "current_step": 4,
            }
        except Exception as e:
            print(f"   ⚠️ Risk failed: {e}")
            traceback.print_exc()
            return {"current_step": 4, "errors": state["errors"] + [f"risk: {e}"]}

    def _framework_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 4: Framework mapping (Sonnet)."""
        llm = config["configurable"]["llm"]
        print("🗺️ Agent 4/9: Framework Mapping...")
        uploaded = state.get("uploaded_frameworks", {})
        text = state["document_text"]
        doc_type = state["document_type"]
        mappings = {}

        # RAG-based comparison for uploaded framework standards
        for fw_key in framework_store.FRAMEWORK_KEYS:
            if uploaded.get(fw_key):
                try:
                    hits = framework_store.search_framework(fw_key, text[:2000], top_k=10)
                    if hits:
                        prompt = framework_comparison_prompt(text, doc_type, fw_key, hits)
                        raw = llm.invoke(prompt, max_tokens=4096)
                        data = llm.parse_json(raw)
                        data['source'] = 'uploaded_standard'
                        mappings[fw_key] = data
                        print(f"   📋 {fw_key}: score={data.get('alignment_score', '?')} (RAG)")
                        continue
                except Exception as e:
                    print(f"   ⚠️ {fw_key} RAG comparison failed: {e}")

        # LLM-based mapping for remaining frameworks
        try:
            remaining = [k for k in framework_store.FRAMEWORK_KEYS if k not in mappings]
            if remaining:
                prompt = framework_mapping_prompt(text, doc_type)
                raw = llm.invoke(prompt, max_tokens=6144)
                data = llm.parse_json(raw)
                for key in remaining:
                    if key in data:
                        data[key]['source'] = 'ai_knowledge'
                        mappings[key] = data[key]
                        print(f"   📋 {key}: score={data[key].get('alignment_score', '?')} (LLM)")
        except Exception as e:
            print(f"   ⚠️ Framework mapping failed: {e}")
            traceback.print_exc()

        return {"framework_mappings": mappings, "current_step": 5}

    def _gap_detection_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 5: Gap detection — document-driven, receives upstream findings (Sonnet)."""
        llm = config["configurable"]["llm"]
        print("🔎 Agent 5/9: Gap Detection...")
        try:
            prompt = gap_detection_prompt(
                state["document_text"], state["document_type"],
                synopsis=state.get("synopsis"),
                compliance_findings=state.get("compliance_findings"),
                security_findings=state.get("security_findings"),
            )
            raw = llm.invoke(prompt, max_tokens=4096)
            data = llm.parse_json(raw)
            gaps = data.get("gaps", [])
            print(f"   ✅ {len(gaps)} gaps detected")
            return {"gap_detections": gaps, "current_step": 6}
        except Exception as e:
            print(f"   ⚠️ Gap detection failed: {e}")
            traceback.print_exc()
            return {"current_step": 6, "errors": state["errors"] + [f"gap_detection: {e}"]}

    def _scoring_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 6: Scoring (fast model)."""
        llm = config["configurable"]["llm"]
        print("📊 Agent 6/9: Scoring...")
        try:
            prompt = scoring_prompt(
                state["document_text"], state["document_type"],
                synopsis=state.get("synopsis")
            )
            raw = llm.invoke_fast(prompt, max_tokens=2048)
            data = llm.parse_json(raw)

            # Compute overall score from all agent scores
            scores = [
                state.get("compliance_score", 0),
                state.get("security_score", 0),
                state.get("risk_score", 0),
            ]
            # Add framework average
            fw_scores = [
                m.get("alignment_score", 0)
                for m in state.get("framework_mappings", {}).values()
                if isinstance(m, dict)
            ]
            if fw_scores:
                scores.append(sum(fw_scores) / len(fw_scores))

            overall = round(sum(scores) / max(len(scores), 1))
            maturity = data.get("document_maturity", "developing")

            # Build human-readable rationale bullets
            score_rationale = [
                f"Compliance Score: {state.get('compliance_score', 0)}/100",
                f"Security Score: {state.get('security_score', 0)}/100",
                f"Risk Score: {state.get('risk_score', 0)}/100",
            ]
            if fw_scores:
                score_rationale.append(f"Framework Avg: {round(sum(fw_scores)/len(fw_scores))}/100")
            for dim in ['completeness', 'security_strength', 'coverage', 'clarity', 'enforcement_level']:
                entry = data.get(dim, {})
                if isinstance(entry, dict) and entry.get('rationale'):
                    score_rationale.append(f"{dim.replace('_',' ').title()}: {entry['rationale']}")

            print(f"   ✅ Overall score={overall}, maturity={maturity}")
            return {
                "scoring_details": data,
                "overall_score": overall,
                "score_rationale": score_rationale,
                "document_maturity": maturity,
                "current_step": 7,
            }
        except Exception as e:
            print(f"   ⚠️ Scoring failed: {e}")
            traceback.print_exc()
            return {"current_step": 7, "errors": state["errors"] + [f"scoring: {e}"]}

    def _best_practices_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 7: Best practices comparison — document-driven (Sonnet)."""
        llm = config["configurable"]["llm"]
        print("🏆 Agent 7/9: Best Practices...")
        try:
            prompt = best_practices_prompt(
                state["document_text"], state["document_type"],
                synopsis=state.get("synopsis"),
                gap_detections=state.get("gap_detections"),
            )
            raw = llm.invoke(prompt, max_tokens=4096)
            data = llm.parse_json(raw)
            comparisons = data.get("comparisons", [])
            print(f"   ✅ {len(comparisons)} comparisons")
            return {"best_practices": comparisons, "current_step": 8}
        except Exception as e:
            print(f"   ⚠️ Best practices failed: {e}")
            traceback.print_exc()
            return {"current_step": 8, "errors": state["errors"] + [f"best_practices: {e}"]}

    def _suggestion_agent(self, state: AnalysisState, config: dict) -> dict:
        """Step 8: Auto-suggestions — receives ALL upstream findings (Sonnet)."""
        llm = config["configurable"]["llm"]
        print("💡 Agent 8/9: Suggestions...")
        try:
            prompt = auto_suggest_prompt(
                state["document_text"], state["document_type"],
                synopsis=state.get("synopsis"),
                compliance_findings=state.get("compliance_findings"),
                security_findings=state.get("security_findings"),
                risk_findings=state.get("risk_findings"),
                gap_detections=state.get("gap_detections"),
                best_practices=state.get("best_practices"),
            )
            raw = llm.invoke(prompt, max_tokens=4096)
            data = llm.parse_json(raw)
            suggestions = data.get("suggestions", [])
            print(f"   ✅ {len(suggestions)} suggestions")
            return {"auto_suggestions": suggestions, "current_step": 9}
        except Exception as e:
            print(f"   ⚠️ Suggestions failed: {e}")
            traceback.print_exc()
            return {"current_step": 9, "errors": state["errors"] + [f"suggestions: {e}"]}

    def _finalize(self, state: AnalysisState, config: dict) -> dict:
        """Step 9: LLM-based synthesis of all findings into deduplicated recommendations."""
        llm = config["configurable"]["llm"]
        print("🧠 Agent 9/9: Synthesis & Recommendations...")
        try:
            prompt = recommendations_prompt(
                document_type=state["document_type"],
                synopsis=state.get("synopsis"),
                compliance_findings=state.get("compliance_findings"),
                security_findings=state.get("security_findings"),
                risk_findings=state.get("risk_findings"),
                gap_detections=state.get("gap_detections"),
                best_practices=state.get("best_practices"),
                suggestions=state.get("auto_suggestions"),
            )
            raw = llm.invoke(prompt, max_tokens=4096)
            data = llm.parse_json(raw)
            recs = data.get("recommendations", [])
            print(f"   ✅ {len(recs)} recommendations synthesized")
            return {"recommendations": recs, "current_step": 10}
        except Exception as e:
            print(f"   ⚠️ Finalize failed, using fallback: {e}")
            traceback.print_exc()
            # Fallback: simple structural recommendations
            recs = self._fallback_recommendations(state)
            return {"recommendations": recs, "current_step": 10,
                    "errors": state["errors"] + [f"finalize: {e}"]}

    # ---- Fallback if LLM-based finalize fails --------------------------------
    @staticmethod
    def _fallback_recommendations(state: AnalysisState) -> list:
        """Generate basic recommendations from findings when LLM synthesis fails."""
        recs = []
        for f in state.get("compliance_findings", []):
            if f.get("severity") in ("high", "critical"):
                recs.append({
                    "action": f"Address compliance issue: {f.get('issue', 'Unknown')}",
                    "priority": "high",
                    "category": "compliance",
                    "effort": "moderate",
                    "rationale": f"Identified as {f.get('severity', 'high')} severity compliance finding.",
                })
        for f in state.get("security_findings", []):
            if f.get("severity") in ("high", "critical"):
                recs.append({
                    "action": f"Remediate security issue: {f.get('issue', 'Unknown')}",
                    "priority": "high",
                    "category": "security",
                    "effort": "moderate",
                    "rationale": f"Identified as {f.get('severity', 'high')} severity security finding.",
                })
        for g in state.get("gap_detections", []):
            if g.get("severity") in ("critical", "high"):
                recs.append({
                    "action": f"Close gap: {g.get('gap_title', 'Unknown')}",
                    "priority": "high",
                    "category": "documentation",
                    "effort": "significant",
                    "rationale": g.get("details", ""),
                })
        return recs[:10]

    # ---- Multi-document batch analysis ---------------------------------------

    def run_batch(self, documents: list[dict], doc_type: str,
                  uploaded_frameworks: dict = None) -> dict:
        """Run analysis on multiple documents with cross-document synthesis.

        Args:
            documents: List of {"id": int, "filename": str, "text": str}
            doc_type: Document type (policy, contract, procedure)
            uploaded_frameworks: Which framework standards are uploaded

        Returns:
            Combined result with individual + cross-doc analysis.
        """
        start = time.time()
        if uploaded_frameworks is None:
            uploaded_frameworks = {k: False for k in framework_store.FRAMEWORK_KEYS}

        print(f"\n{'='*60}")
        print(f"🔄 BATCH ANALYSIS: {len(documents)} documents")
        print(f"{'='*60}\n")
        for idx, d in enumerate(documents):
            print(f"  [{idx}] id={d['id']} filename='{d['filename']}' text_len={len(d.get('text',''))}")

        # Phase 1: Analyze each document individually
        individual_results = []
        for i, doc in enumerate(documents):
            print(f"\n--- Document {i+1}/{len(documents)}: {doc['filename']} ---")
            try:
                result = self.run(doc["id"], doc["text"], doc_type,
                                  uploaded_frameworks=uploaded_frameworks)
                individual_results.append({
                    "document_id": doc["id"],
                    "filename": doc["filename"],
                    "result": result,
                })
            except BaseException as e:
                print(f"❌ Document {i+1}/{len(documents)} ({doc['filename']}) failed: {e}")
                traceback.print_exc()
                # Include a minimal placeholder so cross-doc analysis still runs
                individual_results.append({
                    "document_id": doc["id"],
                    "filename": doc["filename"],
                    "result": {
                        "overall_score": 0, "compliance_score": 0,
                        "security_score": 0, "risk_score": 0,
                        "risk_level": "unknown", "document_maturity": "unknown",
                        "compliance_findings": [], "security_findings": [],
                        "risk_findings": [], "framework_mappings": {},
                        "gap_detections": [], "best_practices": [],
                        "auto_suggestions": [], "recommendations": [],
                        "scoring_details": {}, "synopsis": None,
                        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                        "processing_time": 0, "errors": [str(e)],
                    },
                })
            print(f"📌 Finished document {i+1}/{len(documents)}: {doc['filename']}")

        print(f"\n✅ LOOP COMPLETE: processed {len(individual_results)} of {len(documents)} documents")

        # Phase 2: Cross-document gap detection via vector cross-reference
        print(f"\n{'='*60}")
        print(f"🔗 CROSS-DOCUMENT ANALYSIS")
        print(f"{'='*60}\n")

        cross_doc_gaps = self._cross_doc_gap_detection(individual_results)

        # Phase 3: Unified synthesis
        synthesis = self._multi_doc_synthesis(individual_results)

        # Sum all tokens
        total_batch_tokens = 0
        for ir in individual_results:
            total_batch_tokens += ir["result"].get("total_tokens", 0)
        total_batch_tokens += cross_doc_gaps.get("total_tokens", 0)
        total_batch_tokens += synthesis.get("total_tokens", 0)

        total_time = round(time.time() - start, 2)

        return {
            "individual_results": individual_results,
            "cross_doc_gaps": cross_doc_gaps,
            "synthesis": synthesis, # synthesis already contains total_tokens
            "processing_time": total_time,
            "document_count": len(documents),
            "total_tokens": total_batch_tokens, # Add total_tokens to the batch result
        }

    def _cross_doc_gap_detection(self, individual_results: list) -> dict:
        """Use Vector Cross-Reference to detect inter-document gaps."""
        import chromadb
        from chromadb.config import Settings

        print("🔎 Cross-doc gap detection via Vector Cross-Reference...")

        # Build temporary collection for cross-referencing
        try:
            client = chromadb.Client(Settings(anonymized_telemetry=False))
            col = client.get_or_create_collection(
                "batch_cross_ref", metadata={"hnsw:space": "cosine"}
            )

            # Index each document's text in chunks
            for ir in individual_results:
                text = ir["result"].get("document_text", "")
                filename = ir["filename"]
                if not text:
                    continue
                # Simple chunking for cross-reference
                chunk_size = 1500
                chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size-200)]
                if not chunks:
                    continue
                ids = [f"{filename}_chunk_{i}" for i in range(len(chunks))]
                metadatas = [{"filename": filename, "doc_id": ir["document_id"]} for _ in chunks]
                col.add(documents=chunks, ids=ids, metadatas=metadatas)

            # Build summaries for the gap prompt
            doc_summaries = []
            for ir in individual_results:
                result = ir["result"]
                doc_summaries.append({
                    "filename": ir["filename"],
                    "synopsis": result.get("synopsis"),
                    "gap_detections": result.get("gap_detections", []),
                    "overall_score": result.get("overall_score", 0),
                })

            # For each document's gaps, search across other documents
            all_gap_topics = []
            for summary in doc_summaries:
                for gap in summary.get("gap_detections", []):
                    all_gap_topics.append(gap.get("gap_title", "") + " " + gap.get("details", ""))

            # Search for coverage of gaps across all documents
            cross_doc_chunks = []
            for topic in all_gap_topics[:15]:  # limit queries
                if not topic.strip():
                    continue
                try:
                    results = col.query(query_texts=[topic], n_results=5)
                    docs = results.get("documents", [[]])[0]
                    metas = results.get("metadatas", [[]])[0]
                    for j, doc_text in enumerate(docs):
                        meta = metas[j] if j < len(metas) else {}
                        cross_doc_chunks.append({
                            "text": doc_text[:800],
                            "filename": meta.get("filename", "unknown"),
                        })
                except Exception:
                    pass

            # Deduplicate cross_doc_chunks
            seen = set()
            unique_chunks = []
            for c in cross_doc_chunks:
                key = c["text"][:100]
                if key not in seen:
                    seen.add(key)
                    unique_chunks.append(c)

            # Ask LLM to resolve cross-document gaps
            local_llm = get_llm_client()
            prompt = multi_doc_gap_prompt(doc_summaries, unique_chunks[:20])
            raw = local_llm.invoke(prompt, max_tokens=6144)
            data = local_llm.parse_json(raw)
            data["total_tokens"] = local_llm.total_input_tokens + local_llm.total_output_tokens

            print(f"   ✅ Resolved gaps: {len(data.get('resolved_gaps', []))}")
            print(f"   ✅ Corpus gaps: {len(data.get('corpus_gaps', []))}")
            print(f"   ✅ Contradictions: {len(data.get('contradictions', []))}")

            # Cleanup temp collection
            try:
                client.delete_collection("batch_cross_ref")
            except Exception:
                pass

            return data

        except Exception as e:
            print(f"   ⚠️ Cross-doc gap detection failed: {e}")
            traceback.print_exc()
            return {"resolved_gaps": [], "corpus_gaps": [], "contradictions": [], "total_tokens": 0, "error": str(e)}

    def _multi_doc_synthesis(self, individual_results: list) -> dict:
        """Synthesize all document results into a unified assessment."""
        print("🧠 Multi-document synthesis...")
        try:
            doc_results = []
            for ir in individual_results:
                result = ir["result"]
                doc_results.append({
                    "filename": ir["filename"],
                    "overall_score": result.get("overall_score", 0),
                    "compliance_score": result.get("compliance_score", 0),
                    "security_score": result.get("security_score", 0),
                    "risk_score": result.get("risk_score", 0),
                    "risk_level": result.get("risk_level", "unknown"),
                    "document_maturity": result.get("document_maturity", "unknown"),
                    "gap_count": len(result.get("gap_detections", [])),
                    "top_gaps": [g.get("gap_title", "") for g in result.get("gap_detections", [])[:3]],
                    "top_risks": [r.get("risk", "") for r in result.get("risk_findings", [])[:3]],
                })

            local_llm = get_llm_client()
            prompt = multi_doc_synthesis_prompt(doc_results)
            raw = local_llm.invoke(prompt, max_tokens=4096)
            data = local_llm.parse_json(raw)
            data["total_tokens"] = local_llm.total_input_tokens + local_llm.total_output_tokens

            print(f"   ✅ Synthesis complete: overall_score={data.get('overall_score', '?')}")
            return data

        except Exception as e:
            print(f"   ⚠️ Synthesis failed: {e}")
            traceback.print_exc()
            # Fallback: simple average
            scores = [ir["result"].get("overall_score", 0) for ir in individual_results]
            return {
                "overall_score": round(sum(scores) / max(len(scores), 1)),
                "risk_level": "medium",
                "document_maturity": "developing",
                "executive_summary": "Multi-document synthesis failed. Individual results are available.",
                "error": str(e),
            }
