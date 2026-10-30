"""Built-in skill vocabulary (tech AND non-tech, English/Spanish aliases).

Used to PROPOSE skills from a résumé and to find skill requirements in job descriptions.
Users can add their own skills/aliases in the UI; this list is only a starting point.

Matching rules:
- Text and aliases are lower-cased and accent-folded ("Inglés" == "ingles"), whitespace/hyphen tolerant.
- An alias must not be glued to letters/digits ("java" does not match "javascript").
- One combined regex scans the text once; longer aliases win ("sql server" over "sql").
- Ambiguous words use vetted case-sensitive ``patterns`` instead of plain aliases
  (e.g. "Excel" the product vs "excel at", "React" vs "react quickly", "R" only inside skill lists).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

MAX_SCAN = 100_000

CATEGORY_LABELS = {
    "programming": "Programación", "data": "Ingeniería de datos", "cloud": "Nube", "database": "Bases de datos",
    "bi": "BI y reportes", "ai": "IA / ML", "devops": "DevOps y herramientas", "web": "Desarrollo web y APIs",
    "methods": "Metodologías", "office": "Ofimática y sistemas", "business": "Negocio y gestión",
    "finance": "Finanzas y contabilidad", "sales": "Ventas y marketing", "operations": "Operaciones y logística",
    "hr": "Recursos humanos", "support": "Soporte e infraestructura TI", "education": "Educación y salud",
    "language": "Idiomas", "certification": "Certificaciones", "other": "Otras",
}

# kind: hard = concrete tool/technology/certification (weight 1 when missing from the CV)
#       domain = broader business skill (weight 0.5 when missing; more prone to incidental mentions)
#       language = spoken language (weight 1)
KIND_WEIGHT = {"hard": 1.0, "language": 1.0, "domain": 0.5}


@dataclass(frozen=True)
class VocabEntry:
    key: str
    category: str
    aliases: tuple[str, ...]
    kind: str = "hard"
    patterns: tuple[str, ...] = ()  # case-sensitive regexes on the ORIGINAL text (vetted, never user input)


def _e(key: str, category: str, aliases: str = "", kind: str = "hard", patterns: tuple[str, ...] = ()) -> VocabEntry:
    als = tuple(a.strip() for a in aliases.split("|") if a.strip())
    return VocabEntry(key, category, als, kind, patterns)


_B = r"(?<![A-Za-z0-9])"  # left boundary for case-sensitive patterns
_A = r"(?![A-Za-z0-9])"   # right boundary

VOCABULARY: tuple[VocabEntry, ...] = (
    # ---------------------------------------------------------------- programming
    _e("Python", "programming", "python|python 3|python3"),
    _e("SQL", "programming", "sql"),
    _e("T-SQL", "programming", "t-sql|tsql|transact-sql"),
    _e("PL/SQL", "programming", "pl/sql|plsql"),
    _e("C#", "programming", "c#|c sharp|csharp"),
    _e("Java", "programming", "java"),
    _e("JavaScript", "programming", "javascript|ecmascript"),
    _e("TypeScript", "programming", "typescript"),
    _e("Scala", "programming", "scala"),
    _e("Go", "programming", "golang|go programming|go language|lenguaje go"),
    _e("Rust", "programming", "rust programming|rust language|rustlang", patterns=(_B + r"Rust" + _A,)),
    _e("Kotlin", "programming", "kotlin"),
    _e("Swift", "programming", "swift programming|swiftui", patterns=(_B + r"Swift" + _A,)),
    _e("PHP", "programming", "php"),
    _e("Ruby", "programming", "ruby|ruby on rails|rails"),
    _e("R", "programming", "r programming|r language|rstudio|lenguaje r",
       patterns=(r"(?<![A-Za-z0-9/&(])R(?=\s*(?:,|/|;|\)|\band\b|\bor\b|\by\b))",)),
    _e("MATLAB", "programming", "matlab"),
    _e("VBA", "programming", "vba|visual basic for applications|excel macros|macros de excel"),
    _e("Bash", "programming", "bash|shell scripting|shell scripts|unix shell"),
    _e("PowerShell", "programming", "powershell"),
    _e("C++", "programming", "c++|cpp"),
    _e("COBOL", "programming", "cobol"),
    # ---------------------------------------------------------------- data engineering
    _e("ETL/ELT", "data", "etl|elt|etl/elt|elt/etl"),
    _e("Data pipelines", "data", "data pipeline|data pipelines|pipeline development|pipelines de datos"),
    _e("Data warehousing", "data", "data warehouse|data warehouses|data warehousing|enterprise data warehouse|edw|"
                                   "almacén de datos|almacenes de datos"),
    _e("Data modeling", "data", "data modeling|data modelling|dimensional modeling|dimensional modelling|"
                                "star schema|modelado de datos|modelos de datos"),
    _e("Data integration", "data", "data integration|data integrations|integración de datos"),
    _e("Data quality", "data", "data quality|data validation|calidad de datos|validación de datos"),
    _e("Data governance", "data", "data governance|data stewardship|gobierno de datos|gobernanza de datos"),
    _e("Data lakehouse", "data", "data lake|data lakes|lakehouse|delta lake"),
    _e("Data analysis", "data", "data analysis|data analytics|análisis de datos|analítica de datos"),
    _e("Data migration", "data", "data migration|data migrations|database migration|database migrations|"
                                 "migración de datos|migraciones de datos"),
    _e("Data orchestration", "data", "data orchestration|workflow orchestration|orquestación de datos"),
    _e("SQL performance tuning", "data", "query optimization|sql optimization|query tuning|sql tuning|"
                                         "performance tuning|execution plans|execution plan|"
                                         "optimización de consultas|optimización sql"),
    _e("Stored procedures", "data", "stored procedure|stored procedures|procedimientos almacenados"),
    _e("Apache Airflow", "data", "airflow|apache airflow"),
    _e("Apache Spark", "data", "apache spark|spark sql|spark streaming", patterns=(_B + r"Spark" + _A,)),
    _e("PySpark", "data", "pyspark"),
    _e("Databricks", "data", "databricks|azure databricks"),
    _e("Apache Kafka", "data", "kafka|apache kafka"),
    _e("dbt", "data", "dbt|data build tool"),
    _e("Snowflake", "data", "snowflake|snowpark"),
    _e("Talend", "data", "talend"),
    _e("Informatica", "data", "informatica|powercenter"),
    _e("Ab Initio", "data", "ab initio"),
    _e("SSIS", "data", "ssis|sql server integration services"),
    _e("Fivetran", "data", "fivetran"),
    _e("Hadoop", "data", "hadoop|hdfs|apache hive"),
    _e("pandas", "data", "pandas"),
    _e("NumPy", "data", "numpy"),
    _e("Data science", "ai", "data science|ciencia de datos"),
    # ---------------------------------------------------------------- cloud
    _e("Microsoft Azure", "cloud", "azure|microsoft azure"),
    _e("Azure Data Factory", "cloud", "azure data factory|data factory|adf"),
    _e("Azure Synapse Analytics", "cloud", "azure synapse|azure synapse analytics|synapse analytics|synapse"),
    _e("Azure Blob Storage", "cloud", "azure blob storage|blob storage|azure storage|adls|adls gen2|"
                                      "azure data lake storage"),
    _e("Azure API Management", "cloud", "azure api management|apim"),
    _e("Azure Functions", "cloud", "azure functions|azure function"),
    _e("Azure SQL", "cloud", "azure sql|azure sql database|azure sql db|sql azure"),
    _e("Microsoft Fabric", "cloud", "microsoft fabric|fabric data factory|fabric lakehouse|fabric warehouse"),
    _e("Azure DevOps", "devops", "azure devops|azure pipelines|vsts"),
    _e("AWS", "cloud", "aws|amazon web services"),
    _e("AWS Glue", "cloud", "aws glue"),
    _e("Amazon Redshift", "cloud", "redshift|amazon redshift"),
    _e("Amazon S3", "cloud", "amazon s3|aws s3|s3 bucket|s3 buckets"),
    _e("AWS Lambda", "cloud", "aws lambda"),
    _e("Google Cloud", "cloud", "gcp|google cloud|google cloud platform"),
    _e("BigQuery", "cloud", "bigquery|big query"),
    _e("Cosmos DB", "database", "cosmos db|cosmosdb|azure cosmos db"),
    # ---------------------------------------------------------------- databases
    _e("SQL Server", "database", "sql server|mssql|microsoft sql server|ms sql|ms sql server"),
    _e("PostgreSQL", "database", "postgresql|postgres"),
    _e("MySQL", "database", "mysql"),
    _e("Oracle Database", "database", "oracle|oracle database|oracle db"),
    _e("MongoDB", "database", "mongodb|mongo db|mongo"),
    _e("Redis", "database", "redis"),
    _e("Teradata", "database", "teradata"),
    _e("IBM Db2", "database", "db2|ibm db2"),
    _e("NoSQL", "database", "nosql"),
    _e("Database administration", "database", "database administration|database administrator|dba|"
                                              "administración de bases de datos"),
    # ---------------------------------------------------------------- BI / reporting
    _e("Power BI", "bi", "power bi|powerbi|power bi desktop|power bi service"),
    _e("Tableau", "bi", "tableau"),
    _e("Looker", "bi", "looker|looker studio|google data studio"),
    _e("SSRS", "bi", "ssrs|sql server reporting services|reporting services"),
    _e("SSAS", "bi", "ssas|analysis services|sql server analysis services"),
    _e("Qlik", "bi", "qlik|qlikview|qlik sense"),
    _e("DAX", "bi", "dax"),
    _e("Dashboards", "bi", "dashboard|dashboards|dashboarding|tableros|tableros de control", kind="domain"),
    _e("KPIs", "bi", "kpi|kpis|key performance indicators|performance indicators|indicadores clave|"
                     "indicadores de desempeño", kind="domain"),
    # ---------------------------------------------------------------- AI / ML
    _e("Azure OpenAI", "ai", "azure openai|azure open ai|azure openai service"),
    _e("OpenAI API", "ai", "openai|openai api|gpt-4|gpt-4o|gpt models"),
    _e("Generative AI", "ai", "generative ai|genai|gen ai|ia generativa|llm|llms|large language model|"
                              "large language models"),
    _e("Machine learning", "ai", "machine learning|aprendizaje automático"),
    _e("Microsoft Agent Framework", "ai", "microsoft agent framework|agent framework"),
    _e("Semantic Kernel", "ai", "semantic kernel"),
    _e("LangChain", "ai", "langchain|langgraph"),
    _e("Prompt Flow", "ai", "prompt flow|promptflow"),
    _e("RAG", "ai", "rag|retrieval augmented generation|retrieval-augmented generation"),
    _e("Prompt engineering", "ai", "prompt engineering|ingeniería de prompts"),
    _e("AI agents", "ai", "ai agents|ai agent|agentic ai|multi-agent|agentes de ia"),
    _e("AI integration", "ai", "ai integration|ai integrations|ai service integration|ai service integrations|"
                               "ai workflow integration|integración de ia"),
    _e("MLOps", "ai", "mlops|llmops"),
    _e("scikit-learn", "ai", "scikit-learn|sklearn|scikit learn"),
    _e("TensorFlow", "ai", "tensorflow"),
    _e("PyTorch", "ai", "pytorch"),
    _e("NLP", "ai", "nlp|natural language processing|procesamiento de lenguaje natural"),
    # ---------------------------------------------------------------- DevOps / tools
    _e("Docker", "devops", "docker|docker compose|dockerfile"),
    _e("Kubernetes", "devops", "kubernetes|k8s|aks|eks|gke"),
    _e("Git", "devops", "git"),
    _e("GitHub", "devops", "github|github actions"),
    _e("GitLab", "devops", "gitlab|gitlab ci"),
    _e("CI/CD", "devops", "ci/cd|cicd|continuous integration|continuous delivery|continuous deployment|"
                          "integración continua"),
    _e("Terraform", "devops", "terraform"),
    _e("Jenkins", "devops", "jenkins"),
    _e("Linux", "devops", "linux|ubuntu|red hat|rhel"),
    _e("TFS", "devops", "tfs|team foundation server"),
    _e("Jira", "devops", "jira"),
    _e("Software testing", "devops", "software testing|test automation|automated testing|unit testing|"
                                     "pruebas de software|pruebas unitarias"),
    # ---------------------------------------------------------------- web / APIs
    _e("REST APIs", "web", "rest api|rest apis|restful|restful api|restful apis|rest services|api rest|"
                           "apis rest|api restful"),
    _e("FastAPI", "web", "fastapi|fast api"),
    _e("API integration", "web", "api integration|api integrations|integración de apis|integración de api"),
    _e("ASP.NET", "web", "asp.net|asp.net mvc|asp.net core|asp net mvc"),
    _e(".NET", "web", ".net|dotnet|.net core|.net framework"),
    _e("React", "web", "react.js|reactjs", patterns=(_B + r"React" + _A + r"(?!\s+(?:to|quickly|fast)\b)",)),
    _e("Angular", "web", "angular|angularjs"),
    _e("Node.js", "web", "node.js|nodejs|node js"),
    _e("Flask", "web", "flask"),
    _e("Django", "web", "django"),
    _e("HTML/CSS", "web", "html|html5|css|css3"),
    _e("GraphQL", "web", "graphql"),
    _e("Microservices", "web", "microservices|microservice|microservicios"),
    # ---------------------------------------------------------------- methodologies
    _e("Agile", "methods", "agile|ágil|metodologías ágiles|metodologia agil|agile development"),
    _e("Scrum", "methods", "scrum"),
    _e("Kanban", "methods", "kanban"),
    _e("ITIL", "methods", "itil"),
    _e("Lean Six Sigma", "methods", "six sigma|lean six sigma|lean manufacturing"),
    _e("Incident management", "methods", "incident management|incident resolution|incident response|"
                                         "gestión de incidentes", kind="domain"),
    # ---------------------------------------------------------------- office / business systems
    _e("Microsoft Excel", "office", "microsoft excel|ms excel|advanced excel|excel avanzado|excel spreadsheets|"
                                    "hojas de cálculo|spreadsheets",
       patterns=(_B + r"Excel" + _A + r"(?!\s+(?:in|at|as)\b)",)),
    _e("Microsoft Office", "office", "microsoft office|ms office|office 365|microsoft 365|m365|paquetería office|"
                                     "paquetería de office", kind="domain"),
    _e("Microsoft Word", "office", "microsoft word|ms word", kind="domain"),
    _e("PowerPoint", "office", "powerpoint|power point", kind="domain"),
    _e("SharePoint", "office", "sharepoint"),
    _e("Google Workspace", "office", "google workspace|g suite|gsuite|google sheets|google docs", kind="domain"),
    _e("SAP", "office", "sap|sap erp|sap s/4hana|s/4hana"),
    _e("Salesforce", "office", "salesforce"),
    _e("Microsoft Dynamics 365", "office", "dynamics 365|microsoft dynamics|dynamics crm|dynamics ax|d365"),
    _e("QuickBooks", "office", "quickbooks"),
    _e("ERP systems", "office", "erp|erp systems|sistemas erp", kind="domain"),
    _e("CRM systems", "office", "crm|crm systems|sistemas crm", kind="domain"),
    # ---------------------------------------------------------------- business / management
    _e("Project management", "business", "project management|gestión de proyectos|administración de proyectos",
       kind="domain"),
    _e("Stakeholder management", "business", "stakeholder management|stakeholder engagement|stakeholders|"
                                             "gestión de stakeholders|gestión de partes interesadas", kind="domain"),
    _e("Requirements gathering", "business", "requirements gathering|requirements analysis|gather requirements|"
                                             "business requirements|translate requirements|"
                                             "levantamiento de requerimientos|análisis de requerimientos",
       kind="domain"),
    _e("Business analysis", "business", "business analysis|análisis de negocio", kind="domain"),
    _e("Process improvement", "business", "process improvement|continuous improvement|process optimization|"
                                          "mejora continua|mejora de procesos", kind="domain"),
    _e("Team leadership", "business", "team leadership|people management|team management|leading teams|"
                                      "liderazgo de equipos|gestión de equipos", kind="domain"),
    _e("Budgeting", "business", "budgeting|budget management|presupuestos|gestión de presupuesto", kind="domain"),
    _e("Vendor management", "business", "vendor management|supplier management|gestión de proveedores",
       kind="domain"),
    _e("Strategic planning", "business", "strategic planning|planeación estratégica|planificación estratégica",
       kind="domain"),
    _e("Customer service", "business", "customer service|customer support|atención al cliente|servicio al cliente",
       kind="domain"),
    _e("Technical documentation", "business", "technical documentation|documentation|documentación técnica|"
                                              "documentación", kind="domain"),
    # ---------------------------------------------------------------- finance
    _e("Accounting", "finance", "accounting|contabilidad", kind="domain"),
    _e("Bookkeeping", "finance", "bookkeeping|teneduría de libros", kind="domain"),
    _e("Accounts payable", "finance", "accounts payable|cuentas por pagar", kind="domain"),
    _e("Accounts receivable", "finance", "accounts receivable|cuentas por cobrar", kind="domain"),
    _e("Financial analysis", "finance", "financial analysis|financial modeling|financial modelling|"
                                        "análisis financiero|modelado financiero", kind="domain"),
    _e("Financial reporting", "finance", "financial reporting|financial statements|estados financieros|"
                                         "reportes financieros", kind="domain"),
    _e("Payroll", "finance", "payroll processing|payroll administration|administración de nómina|nómina",
       kind="domain"),
    _e("Financial audit", "finance", "internal audit|financial audit|auditoría interna|auditoría financiera",
       kind="domain"),
    _e("IFRS / GAAP", "finance", "ifrs|gaap|us gaap|niif"),
    # ---------------------------------------------------------------- sales / marketing
    _e("Business development", "sales", "business development|new business development|desarrollo de negocios",
       kind="domain"),
    _e("Sales", "sales", "b2b sales|b2c sales|sales prospecting|inside sales|consultative selling|ventas|"
                         "ventas b2b", kind="domain"),
    _e("Account management", "sales", "account management|key account management|key accounts|gestión de cuentas",
       kind="domain"),
    _e("Digital marketing", "sales", "digital marketing|online marketing|marketing digital", kind="domain"),
    _e("SEO", "sales", "seo|search engine optimization"),
    _e("Social media marketing", "sales", "social media marketing|social media|redes sociales", kind="domain"),
    _e("Market research", "sales", "market research|investigación de mercados", kind="domain"),
    _e("Negotiation", "sales", "negotiation|negotiations|negociación", kind="domain"),
    _e("Google Analytics", "sales", "google analytics|ga4"),
    # ---------------------------------------------------------------- operations / logistics
    _e("Logistics", "operations", "logistics|logística", kind="domain"),
    _e("Supply chain", "operations", "supply chain|supply chain management|cadena de suministro", kind="domain"),
    _e("Inventory management", "operations", "inventory management|inventory control|control de inventarios|"
                                             "gestión de inventarios", kind="domain"),
    _e("Procurement", "operations", "procurement|purchasing|compras|abastecimiento", kind="domain"),
    _e("Warehouse operations", "operations", "warehouse management|warehouse operations|gestión de almacenes|wms",
       kind="domain"),
    _e("Quality assurance", "operations", "quality assurance|quality control|aseguramiento de calidad|"
                                          "control de calidad", kind="domain"),
    _e("Health and safety", "operations", "health and safety|occupational health and safety|ohs|osha|"
                                          "seguridad e higiene|seguridad industrial", kind="domain"),
    _e("Production planning", "operations", "production planning|manufacturing operations|"
                                            "planeación de la producción", kind="domain"),
    # ---------------------------------------------------------------- HR
    _e("Recruiting", "hr", "recruiting|recruitment|talent acquisition|reclutamiento|selección de personal",
       kind="domain"),
    _e("Employee onboarding", "hr", "employee onboarding|onboarding de personal|inducción de personal",
       kind="domain"),
    _e("HR management", "hr", "human resources management|hr management|hr administration|"
                              "gestión de recursos humanos|administración de personal", kind="domain"),
    _e("Employee relations", "hr", "labor relations|labour relations|employee relations|relaciones laborales",
       kind="domain"),
    _e("Training and development", "hr", "training and development|learning and development|staff training|"
                                         "capacitación de personal", kind="domain"),
    # ---------------------------------------------------------------- IT support / infrastructure
    _e("IT support", "support", "it support|technical support|help desk|helpdesk|service desk|desktop support|"
                                "soporte técnico|mesa de ayuda", kind="domain"),
    _e("IT management", "support", "it management|it planning|it operations|gestión de ti|administración de ti",
       kind="domain"),
    _e("Networking", "support", "computer networking|network administration|tcp/ip|lan/wan|redes de computadoras|"
                                "administración de redes"),
    _e("Server administration", "support", "server administration|server monitoring|server maintenance|"
                                           "windows server|administración de servidores"),
    _e("Active Directory", "support", "active directory"),
    _e("Cybersecurity", "support", "cybersecurity|cyber security|information security|seguridad informática|"
                                   "ciberseguridad"),
    _e("Backup and recovery", "support", "backup and recovery|database backups|backups|respaldos|"
                                         "copias de seguridad", kind="domain"),
    # ---------------------------------------------------------------- education / health (non-tech résumés)
    _e("Teaching", "education", "teaching|docencia|instructional design|diseño instruccional|"
                                "curriculum development", kind="domain"),
    _e("Patient care", "education", "patient care|atención al paciente", kind="domain"),
    # ---------------------------------------------------------------- spoken languages
    _e("English", "language", "english|inglés", kind="language"),
    _e("Spanish", "language", "spanish|español|castellano", kind="language"),
    _e("French", "language", "french|français|francés", kind="language"),
    _e("Portuguese", "language", "portuguese|portugués", kind="language"),
    _e("German", "language", "german language|alemán", kind="language",
       patterns=(_B + r"German" + _A + r"(?!\s+[A-Z])",)),
    _e("Italian", "language", "italian|italiano", kind="language"),
    _e("Mandarin Chinese", "language", "mandarin|mandarín|chino mandarín", kind="language"),
    _e("Japanese", "language", "japanese|japonés", kind="language"),
    # ---------------------------------------------------------------- certifications
    _e("Azure Data Engineer Associate (DP-203)", "certification", "dp-203|azure data engineer associate"),
    _e("Azure Fundamentals (AZ-900)", "certification", "az-900|azure fundamentals"),
    _e("Azure AI Engineer Associate (AI-102)", "certification", "ai-102|azure ai engineer"),
    _e("Fabric Analytics Engineer (DP-600)", "certification", "dp-600|fabric analytics engineer"),
    _e("Power BI Data Analyst (PL-300)", "certification", "pl-300|da-100|power bi data analyst"),
    _e("AWS Certified Solutions Architect", "certification", "aws certified solutions architect|"
                                                            "aws solutions architect"),
    _e("SnowPro Core", "certification", "snowpro|snowpro core"),
    _e("PMP", "certification", "pmp|project management professional"),
    _e("Scrum Master certification", "certification", "certified scrummaster|certified scrum master|"
                                                     "professional scrum master|psm"),
    _e("CPA", "certification", "cpa|certified public accountant|contador público certificado"),
)

BY_KEY: dict[str, VocabEntry] = {e.key: e for e in VOCABULARY}
# Display names that are too generic to match on their own; these entries match only via explicit aliases.
_NO_KEY_ALIAS = {"R", "Go", "Networking"}


# ----------------------------------------------------------------------------- text folding
def fold(text: str) -> str:
    """Lower-case + strip accents, keeping a 1:1 character mapping with the original text."""
    out = []
    for ch in text:
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)).lower()
        if len(base) != 1:
            base = ch.lower() if len(ch.lower()) == 1 else ch
        out.append(base)
    return "".join(out)


def norm_phrase(text: str) -> str:
    return re.sub(r"[\s\-]+", " ", fold(text).strip())


def phrase_regex(phrases) -> re.Pattern[str] | None:
    """One alternation over folded phrases, longest first, with alphanumeric boundaries."""
    parts = sorted({norm_phrase(p) for p in phrases if p and str(p).strip()}, key=len, reverse=True)
    if not parts:
        return None
    alts = "|".join(re.escape(p).replace("\\ ", " ").replace(" ", r"[\s\-]+") for p in parts)
    return re.compile(r"(?<![a-z0-9])(?:" + alts + r")(?![a-z0-9+#])")


@lru_cache(maxsize=1)
def _vocab_index() -> tuple[re.Pattern[str], dict[str, str], tuple[tuple[str, re.Pattern[str]], ...]]:
    lookup: dict[str, str] = {}
    phrases = []
    for entry in VOCABULARY:
        names = list(entry.aliases)
        # Ambiguous display names (Excel, React, Spark…) match only via patterns; broad business ("domain")
        # skills only via their specific aliases ("Sales" must not match "sales-portal").
        if not entry.patterns and entry.kind != "domain" and entry.key not in _NO_KEY_ALIAS:
            names.append(entry.key)
        for alias in names:
            lookup.setdefault(norm_phrase(alias), entry.key)
            phrases.append(alias)
    pattern = phrase_regex(phrases)
    special = tuple((e.key, re.compile(p)) for e in VOCABULARY for p in e.patterns)
    return pattern, lookup, special


@lru_cache(maxsize=1)
def _exact_names() -> dict[str, str]:
    out: dict[str, str] = {}
    for e in VOCABULARY:
        for name in (e.key, *e.aliases):
            out.setdefault(norm_phrase(name), e.key)
    return out


def vocab_exact(term: str) -> str | None:
    """Exact name/alias lookup for an isolated term (e.g. one item of a skills list: 'R', 'Go', 'Excel')."""
    return _exact_names().get(norm_phrase(term))


def snippet(text: str, start: int, end: int, width: int = 70) -> str:
    a = max(0, start - width)
    b = min(len(text), end + width)
    s = " ".join(text[a:b].split())
    return ("…" if a > 0 else "") + s + ("…" if b < len(text) else "")


@lru_cache(maxsize=512)
def find_vocab(text: str) -> tuple[tuple[str, int, int], ...]:
    """Return (vocab_key, start, end) for the first hit of every vocabulary entry in ``text``."""
    if not text:
        return ()
    text = text[:MAX_SCAN]
    pattern, lookup, special = _vocab_index()
    folded = fold(text)
    seen: dict[str, tuple[str, int, int]] = {}
    for m in pattern.finditer(folded):
        key = lookup.get(re.sub(r"[\s\-]+", " ", m.group(0)))
        if key and key not in seen:
            seen[key] = (key, m.start(), m.end())
    for key, rx in special:
        if key not in seen:
            m = rx.search(text)
            if m:
                seen[key] = (key, m.start(), m.end())
    return tuple(sorted(seen.values(), key=lambda t: t[1]))
