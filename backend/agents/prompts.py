"""
Prompt templates for every specialized agent in the Document Analyzer system.
Each function returns the full prompt string ready for Bedrock invocation.

Key improvements:
- _MAX_DOC_CHARS raised to 50,000 (~20 pages)
- Synopsis agent extracts document structure before analysis
- Gap detection is document-driven, not checklist-driven
- Best practices compare only areas the document actually covers
- Multi-doc prompts for cross-document analysis
"""

import json

# ---- helper to cap text length -------------------------------------------
_MAX_DOC_CHARS = 50000


def _trim(text: str) -> str:
    if len(text) <= _MAX_DOC_CHARS:
        return text
    # Smart trim: keep beginning, middle sample, and end
    third = _MAX_DOC_CHARS // 3
    mid_start = len(text) // 2 - third // 2
    return (
        text[:third]
        + "\n\n[... content omitted for length ...]\n\n"
        + text[mid_start : mid_start + third]
        + "\n\n[... content omitted for length ...]\n\n"
        + text[-third:]
    )


# ===========================================================================
#  0. SYNOPSIS AGENT (runs first, feeds context to all others)
# ===========================================================================
def synopsis_prompt(document_text: str, document_type: str) -> str:
    return f"""You are an expert document analyst. Read the following {document_type} document carefully and extract a structured synopsis of its contents.

DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

Your job is to understand what this document ACTUALLY contains — its real structure, scope, and subject matter. This synopsis will be used by other analysis agents, so be accurate and thorough.

Return **valid JSON only** in this exact format:
{{
  "document_title": "<title or heading of the document>",
  "stated_purpose": "<what the document claims to cover or achieve>",
  "sections_found": ["<actual section headings or topic areas found in the document>"],
  "key_topics_covered": ["<specific topics actually addressed with substance>"],
  "topics_mentioned_but_shallow": ["<topics referenced but not addressed in depth>"],
  "target_audience": "<who this document is intended for>",
  "industry_sector": "<industry or domain if identifiable>",
  "document_length_assessment": "brief|moderate|comprehensive",
  "references_other_documents": ["<any external documents, policies, or standards referenced>"]
}}

Rules:
- Be factual — only list topics the document ACTUALLY covers, not what it should cover.
- sections_found should reflect the document's actual structure.
- Do NOT invent or hallucinate content that isn't in the document.
- Return ONLY the JSON object, no extra text."""


# ===========================================================================
#  1. COMPLIANCE AGENT
# ===========================================================================
def compliance_prompt(document_text: str, document_type: str, synopsis: dict = None) -> str:
    synopsis_block = ""
    if synopsis:
        synopsis_block = f"""
DOCUMENT SYNOPSIS (for context):
{json.dumps(synopsis, indent=2)}
"""
    return f"""You are a senior compliance auditor. Analyze the following {document_type} document for compliance gaps, missing requirements, and regulatory weaknesses.
{synopsis_block}
DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

CRITICAL RULES:
- Your findings must be SPECIFIC to THIS document. Reference actual sections, clauses, or statements.
- Do NOT produce generic compliance observations that could apply to any document.
- Each finding must cite what the document says (or fails to say) that creates the compliance gap.
- If the document is well-compliant in an area, do NOT fabricate issues.

Return your analysis as **valid JSON only** in this exact format:
{{
  "findings": [
    {{"issue": "<specific compliance gap with document reference>", "severity": "high|medium|low", "section": "<actual section name from the document or N/A>", "framework_hint": "<relevant standard>", "evidence": "<quote or reference from the document>"}}
  ],
  "score": <0-100 integer>
}}

Rules:
- score 100 = fully compliant, 0 = no compliance
- Only include findings that are genuinely present — do NOT pad with generic items
- Return ONLY the JSON object, no extra text."""


# ===========================================================================
#  2. SECURITY AGENT
# ===========================================================================
def security_prompt(document_text: str, document_type: str, synopsis: dict = None) -> str:
    synopsis_block = ""
    if synopsis:
        synopsis_block = f"""
DOCUMENT SYNOPSIS (for context):
{json.dumps(synopsis, indent=2)}
"""
    return f"""You are a cybersecurity expert. Analyze the following {document_type} for security vulnerabilities, weak controls, and potential attack vectors.
{synopsis_block}
DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

CRITICAL RULES:
- Focus on security issues RELEVANT to the document's scope and subject matter.
- Reference specific sections, controls, or statements from the document.
- If the document is not about security (e.g., an HR policy), assess only security aspects relevant to its domain.
- Do NOT flag generic security concerns unrelated to the document's actual content.

Return **valid JSON only**:
{{
  "findings": [
    {{"issue": "<specific security issue with document reference>", "severity": "high|medium|low", "category": "access_control|encryption|network|data_protection|incident_response|other", "evidence": "<quote or reference from the document>"}}
  ],
  "score": <0-100 integer>
}}

Rules:
- score 100 = strongest security posture, 0 = no security
- Only include genuine findings relevant to this document
- Return ONLY the JSON object."""


# ===========================================================================
#  3. RISK AGENT
# ===========================================================================
def risk_prompt(document_text: str, document_type: str, synopsis: dict = None) -> str:
    synopsis_block = ""
    if synopsis:
        synopsis_block = f"""
DOCUMENT SYNOPSIS (for context):
{json.dumps(synopsis, indent=2)}
"""
    return f"""You are a risk management specialist. Analyze the following {document_type} for operational, legal, financial, and reputational risks.
{synopsis_block}
DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

CRITICAL RULES:
- Identify risks that are SPECIFIC to what this document covers and its stated scope.
- Reference actual provisions, gaps, or weaknesses in the document.
- Do NOT list generic risks unrelated to the document's content.

Return **valid JSON only**:
{{
  "findings": [
    {{"risk": "<specific risk with document reference>", "severity": "high|medium|low", "type": "operational|legal|financial|reputational", "likelihood": "high|medium|low", "evidence": "<what in the document creates this risk>"}}
  ],
  "score": <0-100 integer>,
  "risk_level": "low|medium|high|critical"
}}

Rules:
- score 100 = lowest risk, 0 = extreme risk
- Return ONLY the JSON object."""


# ===========================================================================
#  4. FRAMEWORK MAPPING AGENT
# ===========================================================================
def framework_mapping_prompt(document_text: str, document_type: str) -> str:
    return f"""You are a GRC (Governance, Risk, Compliance) expert. Map the following {document_type} against six industry frameworks and assess alignment.

DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

For **each** of these frameworks, provide an alignment score and relevant control mappings:
1. ISO 27001:2022 (latest revision — Annex A has 93 controls in 4 themes)
2. SOC 2
3. NIST Cybersecurity Framework
4. CIS Controls
5. GDPR
6. HIPAA

IMPORTANT — ISO 27001:2022 Annex A Control Structure:
- Theme 1 – Organizational controls (A.5.1 – A.5.37)
- Theme 2 – People controls (A.6.1 – A.6.8)
- Theme 3 – Physical controls (A.7.1 – A.7.14)
- Theme 4 – Technological controls (A.8.1 – A.8.34)
Use ONLY these control IDs (A.5.x, A.6.x, A.7.x, A.8.x). Do NOT use old 2013-era IDs like A.9.x, A.10.x, A.12.x, etc.

Return **valid JSON only**:
{{
  "ISO27001": {{
    "alignment_score": <0-100>,
    "standard_version": "2022",
    "mapped_controls": [
      {{"control_id": "A.5.x", "theme": "Organizational|People|Physical|Technological", "control_name": "...", "status": "met|partial|not_met", "notes": "..."}}
    ]
  }},
  "SOC2": {{
    "alignment_score": <0-100>,
    "mapped_controls": [
      {{"control_id": "CC x.x", "control_name": "...", "status": "met|partial|not_met", "notes": "..."}}
    ]
  }},
  "NIST": {{
    "alignment_score": <0-100>,
    "mapped_controls": [
      {{"control_id": "XX.XX-X", "control_name": "...", "status": "met|partial|not_met", "notes": "..."}}
    ]
  }},
  "CIS": {{
    "alignment_score": <0-100>,
    "mapped_controls": [
      {{"control_id": "CIS X", "control_name": "...", "status": "met|partial|not_met", "notes": "..."}}
    ]
  }},
  "GDPR": {{
    "alignment_score": <0-100>,
    "mapped_controls": [
      {{"article": "Art. X", "requirement": "...", "status": "met|partial|not_met", "notes": "..."}}
    ]
  }},
  "HIPAA": {{
    "alignment_score": <0-100>,
    "mapped_controls": [
      {{"rule": "...", "requirement": "...", "status": "met|partial|not_met", "notes": "..."}}
    ]
  }}
}}

Return ONLY the JSON object."""


# ===========================================================================
#  4b. FRAMEWORK COMPARISON (RAG-based — uses uploaded standard text)
# ===========================================================================
def framework_comparison_prompt(
    document_text: str,
    document_type: str,
    framework_key: str,
    retrieved_sections: list[dict],
) -> str:
    """Compare a document against actual uploaded framework standard sections."""
    sections_text = "\n\n---\n\n".join(
        f"[Section from {s.get('filename', 'unknown')} v{s.get('version', '?')}]\n{s['text']}"
        for s in retrieved_sections
    )

    return f"""You are a GRC (Governance, Risk, Compliance) expert.
Compare the following {document_type} against the **{framework_key}** framework standard.

DOCUMENT UNDER REVIEW:
\"\"\"
{_trim(document_text)}
\"\"\"

RELEVANT SECTIONS FROM THE {framework_key} STANDARD:
\"\"\"
{sections_text}
\"\"\"

Based on the actual standard text provided above, evaluate how well the document aligns with the {framework_key} requirements. For each relevant control or requirement found in the standard sections, assess whether the document meets, partially meets, or does not meet it.

Return **valid JSON only**:
{{
  "alignment_score": <0-100>,
  "standard_version": "<version from source sections>",
  "mapped_controls": [
    {{
      "control_id": "<control ID from the standard>",
      "control_name": "<control/requirement name>",
      "status": "met|partial|not_met",
      "notes": "<specific evidence from the document or explanation of the gap>"
    }}
  ],
  "summary": "<2-3 sentence summary of alignment>"
}}

Return ONLY the JSON object."""


# ===========================================================================
#  4c. SINGLE FRAMEWORK LLM COMPARISON (no uploaded standard — uses LLM knowledge)
# ===========================================================================
def single_framework_llm_prompt(
    document_text: str,
    document_type: str,
    framework_key: str,
) -> str:
    """Compare a document against a single framework using LLM training knowledge."""
    return f"""You are a GRC (Governance, Risk, Compliance) expert.
Evaluate the following {document_type} against the **{framework_key}** framework standard using your expert knowledge.

DOCUMENT UNDER REVIEW:
\"\"\"
{_trim(document_text)}
\"\"\"

Assess how well the document aligns with {framework_key} requirements. For each relevant control or requirement, evaluate whether the document meets, partially meets, or does not meet it.

Return **valid JSON only**:
{{
  "alignment_score": <0-100>,
  "mapped_controls": [
    {{
      "control_id": "<control ID>",
      "control_name": "<control/requirement name>",
      "status": "met|partial|not_met",
      "notes": "<specific evidence from the document or explanation of the gap>"
    }}
  ],
  "summary": "<2-3 sentence summary of alignment>"
}}

Return ONLY the JSON object."""


# ===========================================================================
#  5. GAP DETECTION AGENT — Document-driven, not checklist-driven
# ===========================================================================
def gap_detection_prompt(document_text: str, document_type: str,
                         synopsis: dict = None,
                         compliance_findings: list = None,
                         security_findings: list = None) -> str:
    synopsis_block = ""
    if synopsis:
        synopsis_block = f"""
DOCUMENT SYNOPSIS:
{json.dumps(synopsis, indent=2)}
"""
    findings_block = ""
    if compliance_findings or security_findings:
        prior = {}
        if compliance_findings:
            prior["compliance_findings"] = compliance_findings[:8]
        if security_findings:
            prior["security_findings"] = security_findings[:8]
        findings_block = f"""
FINDINGS FROM PRIOR ANALYSIS (for context — do NOT repeat these, find NEW gaps):
{json.dumps(prior, indent=2)}
"""

    return f"""You are an expert policy gap analyst. Perform a thorough review of the following {document_type} and identify significant policy or procedural gaps.
{synopsis_block}{findings_block}
DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

CRITICAL RULES:
1. Analyze what the document ACTUALLY covers and find gaps relative to its stated scope and purpose.
2. Every gap MUST be tied to something THIS document says, implies, or fails to address given its own scope.
3. Do NOT use a generic checklist. DO NOT flag generic items like "missing encryption policy" or "no password requirements" unless the document's scope specifically warrants it.
4. Reference specific sections, clauses, or topic areas from the document.
5. Do NOT repeat findings already identified by the compliance or security agents above.
6. If the document references other documents or procedures, flag any that appear to be missing or broken references.

For each gap, explain:
- What SPECIFIC area the document fails to address (relative to its own stated scope)
- What section or context in the document reveals this gap
- An actionable recommendation to close the gap

Return the top gaps (at most 10) ordered by severity (critical first).

Return **valid JSON only**:
{{
  "gaps": [
    {{
      "gap_title": "<concise, specific name of the gap>",
      "severity": "critical|high|medium|low",
      "details": "<what is missing or weak — reference the document's own content/scope>",
      "document_section": "<which section or area of the document this relates to>",
      "recommendation": "<specific, actionable step to close this gap>"
    }}
  ]
}}

Return ONLY the JSON object."""


# ===========================================================================
#  6. SCORING AGENT
# ===========================================================================
def scoring_prompt(document_text: str, document_type: str, synopsis: dict = None) -> str:
    synopsis_block = ""
    if synopsis:
        synopsis_block = f"""
DOCUMENT SYNOPSIS (for context):
{json.dumps(synopsis, indent=2)}
"""
    return f"""You are an expert document quality assessor. Score the following {document_type} on five dimensions.
{synopsis_block}
DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

Return **valid JSON only**:
{{
  "completeness": {{
    "score": <0-100>,
    "rationale": "<specific explanation referencing document content>"
  }},
  "security_strength": {{
    "score": <0-100>,
    "rationale": "<specific explanation referencing document content>"
  }},
  "coverage": {{
    "score": <0-100>,
    "rationale": "<specific explanation referencing document content>"
  }},
  "clarity": {{
    "score": <0-100>,
    "rationale": "<specific explanation referencing document content>"
  }},
  "enforcement_level": {{
    "score": <0-100>,
    "rationale": "<specific explanation referencing document content>"
  }},
  "document_maturity": "basic|developing|established|mature|optimized"
}}

Definitions:
- completeness: does the doc cover all necessary topics for its stated scope?
- security_strength: how robust are security measures described?
- coverage: breadth of scenarios and edge cases addressed
- clarity: is the language clear, unambiguous, and actionable?
- enforcement_level: are there enforcement mechanisms, penalties, audits?

Return ONLY the JSON object."""


# ===========================================================================
#  7. BEST PRACTICES AGENT — Document-driven comparisons
# ===========================================================================
def best_practices_prompt(document_text: str, document_type: str,
                          synopsis: dict = None,
                          gap_detections: list = None) -> str:
    synopsis_block = ""
    if synopsis:
        synopsis_block = f"""
DOCUMENT SYNOPSIS:
{json.dumps(synopsis, indent=2)}
"""
    gaps_block = ""
    if gap_detections:
        gaps_block = f"""
GAPS ALREADY IDENTIFIED (do NOT repeat these — focus on best practice comparisons instead):
{json.dumps(gap_detections[:5], indent=2)}
"""

    return f"""You are an industry best-practices consultant. Compare the following {document_type} against current industry best practices.
{synopsis_block}{gaps_block}
DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

CRITICAL RULES:
1. ONLY compare areas that this document ACTUALLY addresses. Do NOT compare areas it doesn't cover.
2. For "current_state", QUOTE or closely paraphrase what the document actually says.
3. For "best_practice", cite a specific industry standard or widely-accepted practice.
4. Do NOT use vague comparisons. Be specific about what's different.
5. Do NOT repeat gaps already identified above.

Return **valid JSON only**:
{{
  "comparisons": [
    {{
      "area": "<specific topic area from the document>",
      "current_state": "<what the document actually says — quote or paraphrase>",
      "best_practice": "<what industry best practice recommends, with source>",
      "gap": "high|medium|low|none",
      "recommendation": "<specific, actionable improvement>"
    }}
  ]
}}

Return ONLY the JSON object."""


# ===========================================================================
#  8. AUTO-SUGGEST AGENT — Context-aware suggestions
# ===========================================================================
def auto_suggest_prompt(
    document_text: str,
    document_type: str,
    synopsis: dict = None,
    compliance_findings: list = None,
    security_findings: list = None,
    risk_findings: list = None,
    gap_detections: list = None,
    best_practices: list = None,
) -> str:
    synopsis_block = ""
    if synopsis:
        synopsis_block = f"""
DOCUMENT SYNOPSIS:
{json.dumps(synopsis, indent=2)}
"""
    context = {}
    if compliance_findings:
        context["compliance_findings"] = compliance_findings[:8]
    if security_findings:
        context["security_findings"] = security_findings[:8]
    if risk_findings:
        context["risk_findings"] = risk_findings[:8]
    if gap_detections:
        context["gap_detections"] = gap_detections[:5]
    if best_practices:
        context["best_practice_gaps"] = [
            bp for bp in best_practices[:5] if bp.get("gap") in ("high", "medium")
        ]

    return f"""You are a senior policy consultant. Based on the document and all the analysis findings below, generate specific improvement suggestions.
{synopsis_block}
DOCUMENT TYPE: {document_type}

DOCUMENT:
\"\"\"
{_trim(document_text)}
\"\"\"

ALL PRIOR ANALYSIS FINDINGS:
{json.dumps(context, indent=2)}

Generate specific, actionable suggestions that address the issues found above. Each suggestion should:
1. Reference a specific finding or gap from the analysis
2. Provide concrete language or clauses that could be added
3. Explain WHY this improvement matters

Categories:
- policy_improvement: strengthen existing policy language
- missing_clause: add entirely new sections or clauses
- better_wording: improve clarity, reduce ambiguity
- security_enhancement: strengthen security controls

Return **valid JSON only**:
{{
  "suggestions": [
    {{
      "type": "policy_improvement|missing_clause|better_wording|security_enhancement",
      "title": "<specific, descriptive title>",
      "description": "<detailed suggestion referencing specific findings>",
      "priority": "high|medium|low",
      "addresses_finding": "<which finding/gap this suggestion addresses>",
      "example_text": "<example clause or wording to add>"
    }}
  ]
}}

Rules:
- Every suggestion must address a specific finding from the analysis above
- Do NOT include generic suggestions unrelated to the actual findings
- Return ONLY the JSON object."""


# ===========================================================================
#  9. RECOMMENDATIONS SYNTHESIS AGENT (LLM-based finalization)
# ===========================================================================
def recommendations_prompt(
    document_type: str,
    synopsis: dict = None,
    compliance_findings: list = None,
    security_findings: list = None,
    risk_findings: list = None,
    gap_detections: list = None,
    best_practices: list = None,
    suggestions: list = None,
) -> str:
    """Synthesize ALL findings into a prioritized, deduplicated action plan."""
    context = {
        "document_type": document_type,
    }
    if synopsis:
        context["document_title"] = synopsis.get("document_title", "Unknown")
        context["stated_purpose"] = synopsis.get("stated_purpose", "Unknown")
    if compliance_findings:
        context["compliance_findings"] = compliance_findings[:8]
    if security_findings:
        context["security_findings"] = security_findings[:8]
    if risk_findings:
        context["risk_findings"] = risk_findings[:8]
    if gap_detections:
        context["gap_detections"] = gap_detections[:8]
    if best_practices:
        context["best_practice_gaps"] = [
            bp for bp in best_practices[:5] if bp.get("gap") in ("high", "medium")
        ]
    if suggestions:
        context["suggestions"] = suggestions[:8]

    return f"""You are a senior consultant preparing a final prioritized action plan. You have received the complete analysis of a {document_type} document from multiple specialized agents.

COMPLETE ANALYSIS RESULTS:
{json.dumps(context, indent=2)}

Your job is to SYNTHESIZE all the above findings into a clean, prioritized, DEDUPLICATED action plan. Many findings overlap across agents — merge them.

Rules:
1. DEDUPLICATE: If compliance, security, and gap agents all flagged the same issue, merge into ONE recommendation.
2. PRIORITIZE: Order by impact and urgency, not by agent.
3. BE SPECIFIC: Each action must describe exactly what to do (not just "address gap X").
4. ESTIMATE EFFORT: Classify each action's effort level.
5. GROUP: Group related actions together under a theme.

Return **valid JSON only**:
{{
  "recommendations": [
    {{
      "action": "<specific, actionable step>",
      "priority": "critical|high|medium|low",
      "category": "<theme: security|compliance|governance|operational|documentation>",
      "effort": "quick_win|moderate|significant",
      "rationale": "<why this matters — which findings support it>"
    }}
  ]
}}

Return ONLY the JSON object."""


# ===========================================================================
#  10. MULTI-DOC: Cross-Document Gap Detection
# ===========================================================================
def multi_doc_gap_prompt(
    doc_summaries: list[dict],
    cross_doc_chunks: list[dict],
) -> str:
    """Identify gaps across a corpus of documents, resolving cross-references.

    Args:
        doc_summaries: List of {filename, synopsis, gap_detections} per document.
        cross_doc_chunks: Relevant chunks from vector search across all docs.
    """
    summaries_json = json.dumps(doc_summaries, indent=2)

    chunks_text = "\n\n---\n\n".join(
        f"[From: {c.get('filename', 'unknown')}]\n{c['text']}"
        for c in cross_doc_chunks[:20]
    )

    return f"""You are a senior policy analyst reviewing an organization's complete document set. You have received individual analysis results for each document, plus relevant cross-referenced content.

INDIVIDUAL DOCUMENT ANALYSES:
{summaries_json}

CROSS-REFERENCED CONTENT FROM ALL DOCUMENTS:
\"\"\"
{chunks_text}
\"\"\"

Your task:
1. Review the gaps identified in each individual document.
2. For each gap, check if ANY OTHER DOCUMENT in the set already covers that gap.
3. If another document covers the gap, mark it as "covered" and cite which document covers it.
4. Identify any CROSS-DOCUMENT gaps — topics that NO document in the set addresses.
5. Flag any CONTRADICTIONS between documents.

Return **valid JSON only**:
{{
  "resolved_gaps": [
    {{
      "original_gap": "<gap title from individual analysis>",
      "source_document": "<filename where gap was found>",
      "status": "covered|still_open|partially_covered",
      "covered_by": "<filename that covers this gap, or null>",
      "evidence": "<how the other document covers/addresses this gap>",
      "notes": "<any additional context>"
    }}
  ],
  "corpus_gaps": [
    {{
      "gap_title": "<topic not covered by ANY document>",
      "severity": "critical|high|medium|low",
      "details": "<what's missing from the entire document set>",
      "recommendation": "<what document or section should be created>"
    }}
  ],
  "contradictions": [
    {{
      "topic": "<area of contradiction>",
      "document_a": "<filename>",
      "document_a_says": "<what doc A says>",
      "document_b": "<filename>",
      "document_b_says": "<what doc B says>",
      "recommendation": "<how to resolve>"
    }}
  ]
}}

Return ONLY the JSON object."""


# ===========================================================================
#  11. MULTI-DOC: Synthesis / Combined Score
# ===========================================================================
def multi_doc_synthesis_prompt(doc_results: list[dict]) -> str:
    """Combine all individual document results into a unified organizational assessment."""
    results_json = json.dumps(doc_results, indent=2)

    return f"""You are a senior security consultant. You have received analysis results from {len(doc_results)} policy documents that together form an organization's security and compliance posture.

INDIVIDUAL DOCUMENT RESULTS:
{results_json}

Provide a unified organizational assessment:

Return **valid JSON only**:
{{
  "overall_score": <0-100 weighted average considering all documents>,
  "risk_level": "low|medium|high|critical",
  "document_maturity": "basic|developing|established|mature|optimized",
  "coverage_summary": {{
    "well_covered_areas": ["<areas multiple documents address well>"],
    "weakly_covered_areas": ["<areas only superficially covered>"],
    "uncovered_areas": ["<important areas no document addresses>"]
  }},
  "top_priorities": [
    {{
      "action": "<most important organizational action>",
      "priority": "critical|high|medium",
      "affected_documents": ["<which documents need updates>"],
      "rationale": "<why this is a top priority>"
    }}
  ],
  "strengths": ["<what the organization does well based on these documents>"],
  "score_rationale": ["<bullet-point explaining how the cumulative overall_score was derived, e.g. which documents pulled it up or down and why>"],
  "executive_summary": "<3-5 sentence executive summary of the organization's posture>"
}}

Return ONLY the JSON object."""


# ===========================================================================
#  KNOWLEDGE BASE CHAT (RAG with Citations)
# ===========================================================================
def standalone_question_prompt(chat_history: list, user_message: str) -> str:
    """Generate a standalone question based on history and new input."""
    history_str = ""
    for msg in chat_history[-6:]:
        role = "Human" if msg['role'] == 'user' else "Assistant"
        content = msg['message'][:500] + "..." if len(msg['message']) > 500 else msg['message']
        history_str += f"{role}: {content}\n"

    return f"""Given the following conversation history and a follow-up question, rephrase the follow-up question to be a standalone question.
    
Chat History:
\"\"\"
{history_str}
\"\"\"

Follow Up Input: \"{user_message}\"

Instructions:
1. If the follow-up input refers to the history (e.g., "explain above", "what about access?", "details?"), use the context to formulate a complete question.
2. If the follow-up input is already a standalone question, returns it as is.
3. Output ONLY the standalone question. Do not include prefixes like "Standalone Question:".

Standalone Question:"""

def knowledge_chat_prompt(retrieved_chunks: list, chat_history: list, user_message: str) -> str:
    """Build a RAG prompt from retrieved vector-search chunks with citation instructions."""
    history_str = ""
    for msg in chat_history[-10:]:
        history_str += f"{msg['role'].upper()}: {msg['message']}\n"

    # Build context from retrieved chunks, grouped by source
    context_parts = []
    source_filenames = set()
    for i, chunk in enumerate(retrieved_chunks):
        fname = chunk['filename']
        source_filenames.add(fname)
        context_parts.append(
            f"[Chunk {i+1} | Source: {fname}]\n{chunk['text']}"
        )
    context_str = "\n\n".join(context_parts)

    # Build explicit source list for the LLM
    sources_list = "\n".join(f"  - {fname}" for fname in sorted(source_filenames))

    return f"""You are an intelligent knowledge base assistant. The user is asking questions about their saved policy documents. Below are the most relevant passages retrieved from the knowledge base, each labeled with its source document.

RETRIEVED CONTEXT:
\"\"\"
{context_str}
\"\"\"

AVAILABLE SOURCE DOCUMENTS (use ONLY these exact names for citations):
{sources_list}

CONVERSATION HISTORY:
{history_str}

USER QUESTION: {user_message}

Instructions:
- Answer based ONLY on the retrieved context above
- For EVERY claim or piece of information, include a citation in the format [Source: filename] at the end of the sentence or paragraph
- IMPORTANT: You MUST use ONLY the exact document filenames listed under "AVAILABLE SOURCE DOCUMENTS" above. NEVER invent, shorten, or paraphrase source names
- If multiple chunks from different documents support the answer, cite all relevant sources
- If the context does not contain enough information to answer, say so clearly
- Be concise but thorough
- Use bullet points for lists
- AVOID using markdown headers (like ###) unless absolutely necessary for major section breaks
- AVOID excessive bolding (like **Text**). Use it only for key terms, not entire sentences
- Keep the response clean and easy to read
- Cite sources naturally at the end of relevant sentences
- If information comes from multiple policies, compare and contrast them

Provide your answer with citations:"""


def extract_questions_prompt(text: str) -> str:
    """Prompt to extract a list of questions from a text document."""
    return f"""Analyze the following text and extract all questions found within it.
Return the result strictly as a JSON list of strings. Do not include any other text or markdown formatting.

Text:
\"\"\"
{text[:20000]}
\"\"\"

Output Format:
["Question 1?", "Question 2?"]

JSON Output:"""


def batch_question_answer_prompt(retrieved_chunks: list, question: str) -> str:
    """Build a RAG prompt for answering a single question in batch mode, using retrieved KB chunks."""
    context_parts = []
    for i, chunk in enumerate(retrieved_chunks):
        context_parts.append(
            f"[Chunk {i+1} | Source: {chunk['filename']}]\n{chunk['text']}"
        )
    context_str = "\n\n".join(context_parts)

    return f"""You are an intelligent knowledge base assistant. Answer the user's question using the most relevant passages retrieved from the knowledge base below. Each passage is labeled with its source document.

RETRIEVED CONTEXT:
\"\"\"
{context_str}
\"\"\"

QUESTION: {question}

Instructions:
- Answer based ONLY on the retrieved context above
- For EVERY claim or piece of information, include a citation in the format [Source: filename] at the end of the sentence or paragraph
- If multiple chunks from different documents support the answer, cite all relevant sources
- If the context does not contain enough information to answer fully, provide whatever partial answer is possible from the available context, and note what is missing
- Be concise but thorough
- Use bullet points for lists

Provide your answer with citations:"""
