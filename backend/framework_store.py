"""
Framework Store — ChromaDB collections for compliance framework standards.

Each framework (CIS, GDPR, HIPAA, ISO27001, NIST, SOC2) gets its own
ChromaDB collection so that during analysis the orchestrator can retrieve
only the sections relevant to the specific framework being evaluated.
"""

import os
import chromadb
from chromadb.config import Settings
from config import Config

FRAMEWORK_KEYS = (
    # International Standards
    'ISO27001', 'ISO27002', 'ISO27005', 'ISO27701', 'ISO22301', 'ISO31000',
    # USA
    'NIST', 'NIST_800_53', 'NIST_800_171', 'HIPAA', 'SOX', 'FISMA', 'FedRAMP',
    'CCPA', 'CPRA', 'GLBA', 'FERPA', 'COPPA', 'TSC', 'CJIS',
    # Europe - Extended
    'GDPR', 'NIS2', 'DORA', 'AI_ACT', 'eIDAS', 'UK_GDPR', 'UK_DPA', 'UK_NIS',
    'PSD2', 'EMD', 'CSRD', 'SFDR', 'MiFID_II', 'MiCA', 'UK_SMCR', 'UK_SYSC',
    # Canada
    'PIPEDA', 'CANADA_PRIVACY',
    # Australia & NZ
    'ACSC_E8', 'AU_PRIVACY', 'APRA_CPS234', 'AU_ISM', 'NZ_PRIVACY',
    # Asia-Pacific
    'SG_PDPA', 'TH_PDPA', 'ID_PDPA', 'JP_APPI', 'CN_PIPL', 'MY_PDPA',
    'PH_PDPA', 'IN_IT_ACT', 'IN_SPDI', 'CERT_IN', 'HK_PDPO', 'KR_PIPA',
    'TW_PDPA', 'VN_CYBER_LAW', 'BD_DPA',
    # India Specific
    'IN_DPDP', 'IN_NCIIPC', 'IN_NIST', 'IN_MHA_CYBER', 'IN_MCA', 'IN_RBI',
    'IN_SEBI', 'IN_TRAI', 'IN_UIDAI', 'IN_IRDAI',
    # KSA (Kingdom of Saudi Arabia) - Extended
    'SAMA', 'SAMA_CSF', 'SAMA_BCM', 'SAMA_IT_GOV', 'SAMA_RISK', 'SAMA_OPS',
    'KSA_PDPL', 'KSA_NCA_ECC', 'KSA_NCA_IOT', 'KSA_CLOUD', 'KSA_CRITICAL',
    # UAE - Extended
    'UAE_PDPL', 'UAE_NESA', 'UAE_IAR', 'UAE_CIA', 'UAE_CLOUD', 'UAE_IOT',
    'UAE_DIFC', 'UAE_ADGM', 'UAE_TDRA', 'UAE_CB UAE',
    # Qatar
    'QA_QCB', 'QA_NCSC', 'QA_CLOUD', 'QA_CRITICAL',
    # Bahrain
    'BH_PDPL', 'BH_CBB', 'BH_CLOUD', 'BH_NCSC',
    # Kuwait
    'KW_CSF', 'KW_CBK', 'KW_CLOUD',
    # Oman
    'OM_PDPL', 'OM_CBO', 'OM_CLOUD',
    # Egypt
    'EG_DPL', 'EG_CBE', 'EG_CLOUD', 'EG_NTRA',
    # Jordan
    'JO_CYBER_LAW', 'JO_CBJ',
    # Lebanon
    'LB_CYBER', 'LB_BDL',
    # Iraq
    'IQ_CBI',
    # Africa
    'ZA_POPIA', 'NG_DPA', 'KE_DPA', 'EG_DPL', 'GH_DPA', 'RW_DPA',
    # Latin America
    'BR_LGPD', 'MX_LFPDPPP', 'AR_PDPA', 'CL_LAW', 'CO_LAW', 'PE_LAW',
    # Industry Specific
    'CIS', 'PCIDSS', 'SWIFT_CSP', 'SOC2', 'SOC1', 'SOC3',
    'BASEL', 'SOLVENCY_II', 'MAR', 'CRR', 'CRD', 'FATF',
    'FIPS_140', 'CMMC', 'NERC_CIP', 'TISAX', 'HIPAA_HITECH'
)

# ---- Singleton client --------------------------------------------------------
_client = None


def _get_client():
    global _client
    if _client is None:
        persist_dir = os.path.join(Config.CHROMADB_PATH, "frameworks")
        os.makedirs(persist_dir, exist_ok=True)
        _client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
    return _client


def _get_collection(framework_key: str):
    """Get or create the collection for a specific framework."""
    client = _get_client()
    return client.get_or_create_collection(
        name=f"framework_{framework_key}",
        metadata={"hnsw:space": "cosine"},
    )


# ---- Chunking ----------------------------------------------------------------
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    text = text.strip()
    while start < len(text):
        end = start + CHUNK_SIZE
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ---- Public API --------------------------------------------------------------

def add_framework(framework_key: str, version: str, filename: str, text: str) -> int:
    """Chunk and embed a framework document. Returns chunk count."""
    col = _get_collection(framework_key)

    chunks = _chunk_text(text)
    if not chunks:
        return 0

    # Use version+filename for unique IDs so multiple versions coexist
    prefix = f"{framework_key}_{version}_{filename}"
    ids = [f"{prefix}_chunk{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "framework_key": framework_key,
            "version": version,
            "filename": filename,
            "chunk_index": i,
        }
        for i in range(len(chunks))
    ]

    col.add(documents=chunks, ids=ids, metadatas=metadatas)
    print(f"📋 Indexed {len(chunks)} chunks for {framework_key} v{version} ({filename})")
    return len(chunks)


def remove_framework(framework_key: str, version: str, filename: str):
    """Remove all chunks for a specific framework version/file."""
    col = _get_collection(framework_key)
    try:
        # Delete by matching version AND filename
        col.delete(where={
            "$and": [
                {"version": {"$eq": version}},
                {"filename": {"$eq": filename}},
            ]
        })
        print(f"🗑️ Removed chunks for {framework_key} v{version} ({filename})")
    except Exception as e:
        print(f"Warning: could not remove framework chunks: {e}")


def search_framework(framework_key: str, query: str, top_k: int = 8) -> list[dict]:
    """Search within a specific framework's collection for relevant sections."""
    col = _get_collection(framework_key)
    if col.count() == 0:
        return []

    results = col.query(
        query_texts=[query],
        n_results=min(top_k, col.count()),
    )

    hits = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for i in range(len(docs)):
        meta = metas[i] if i < len(metas) and metas[i] else {}
        hits.append({
            "text": docs[i],
            "version": meta.get("version", "unknown"),
            "filename": meta.get("filename", "unknown"),
            "distance": dists[i] if i < len(dists) else None,
        })
    return hits


def get_uploaded_frameworks() -> dict:
    """
    Return a dict: { framework_key: bool } indicating which frameworks
    have at least one document indexed.
    """
    client = _get_client()
    status = {}
    for key in FRAMEWORK_KEYS:
        try:
            col = client.get_or_create_collection(
                name=f"framework_{key}",
                metadata={"hnsw:space": "cosine"},
            )
            status[key] = col.count() > 0
        except Exception:
            status[key] = False
    return status
