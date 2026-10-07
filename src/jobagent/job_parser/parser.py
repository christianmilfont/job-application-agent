"""Extrai estrutura de uma vaga: seções, tecnologias obrigatórias/desejáveis, senioridade,
modalidade, localização, anos de experiência, inglês, formação e certificações.
Determinístico (regex + taxonomia) — explicável e testável. Um LLM pode refinar depois."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..job_collector.base import RawJob
from .taxonomy import Taxonomy
from .text import BULLET, HEADING, html_to_text, normalize_lines

I = re.IGNORECASE

# --- Cabeçalhos de seção (ordem importa: "requisitos desejáveis" é DESIRED)
_DESIRED_HEAD = re.compile(
    r"diferencia|desej[aá]ve|nice[- ]to[- ]have|\bplus\b|ser[aá] um|b[oô]nus|preferred|good to have|"
    r"would be (great|nice)|seria (legal|[oó]timo)|ganha pontos|pontos extras|n[aã]o obrigat", I)
_REQUIRED_HEAD = re.compile(
    r"requisito|requirement|qualifica|o que (esperamos|buscamos|procuramos)|"
    r"o que voc[eê] (precisa|deve|traz)|voc[eê] precisa|precisa ter|must[- ]have|"
    r"what you('ll)? need|what we('re| are)? looking for|what you bring|voc[eê] (tem|possui)|"
    r"compet[eê]ncias|conhecimentos|obrigat[oó]ri|skills|experi[eê]ncias? (necess|exigid)|"
    r"\bperfil\b|who you are|about you|sobre voc[eê]|vai precisar|habilidades|"
    r"para (essa|esta) (vaga|posi)|esperamos que", I)
_OTHER_HEAD = re.compile(
    r"responsabilidade|atividades|atribui[cç]|o que voc[eê] (vai|ir[aá]) fazer|what you('ll| will) do|"
    r"desafios|sobre (a|o|n[oó]s)|about (us|the)|benef[ií]cio|benefits|oferecemos|what we offer|"
    r"local de trabalho|nossa cultura|quem somos|processo seletivo|etapas|miss[aã]o|dia a dia|"
    r"day[- ]to[- ]day|your role|the role|sobre a vaga|perks|why join|por que|como [eé] trabalhar", I)
_DESIRED_INLINE = re.compile(
    r"diferencial|desej[aá]vel|nice to have|is a plus|[eé] um plus|ser[aá] um diferencial|"
    r"preferencialmente|bonus|b[oô]nus|preferred|ganha pontos|n[aã]o obrigat", I)

# --- Senioridade. Palavras explícitas têm prioridade; entre elas, a MAIS BAIXA vence
# ("Pleno/Sênior" -> pleno). Numerais romanos (I/II/III) só valem se não houver palavra.
_SENIORITY = [
    ("intern", re.compile(r"\b(est[aá]gio|estagi[aá]ri[oa]|intern|internship)\b", I)),
    ("trainee", re.compile(r"\btrainee\b", I)),
    ("junior", re.compile(r"\b(j[uú]nior|jr\.?|entry[- ]level)(?![\w])", I)),
    ("pleno", re.compile(r"\b(pleno|pl|mid[- ]level|mid)\b", I)),
    ("senior", re.compile(r"\b(s[eê]nior|sr\.?|master)(?![\w])|arquitet|architect", I)),
    ("lead", re.compile(r"\b(lead|leader|l[ií]der|staff|principal|especialista|specialist|spec|head|"
                        r"coordenador|coordenadora|coordinator|gerente|manager|diretor|director|executive)\b", I)),
]
_ROMAN = [("junior", re.compile(r"\bI\b(?!\w)")), ("pleno", re.compile(r"\bII\b(?!I)")),
          ("senior", re.compile(r"\b(III|IV)\b"))]
_SENIORITY_IN_TEXT = re.compile(
    r"(n[ií]vel|senioridade|level|posi[cç][aã]o)\s*:?\s*(j[uú]nior|pleno|s[eê]nior|trainee|est[aá]gio)", I)

# Linha com alternativas: "C#, Java, Go ou Rust", "AWS e/ou Azure", "one of"
_ALTERNATIVES = re.compile(r"\b(ou|or)\b|e/ou|and/or|uma (ou mais|das)|one of|alguma|qualquer", I)

# --- Modalidade
_HYBRID = re.compile(r"h[ií]brid|hybrid|\b\d\s*x\s*(por|na|/)\s*semana|\b\d\s+dias?\s+(por|na)\s+semana", I)
_REMOTE = re.compile(r"remot|home[- ]office|anywhere|work from home|trabalho a dist[aâ]ncia", I)
_ONSITE = re.compile(r"presencial|on-?site|in[- ]office|no escrit[oó]rio", I)

# --- Localização
_SP = re.compile(r"s[aã]o paulo|\bSP\b", re.I)
_BR = re.compile(
    r"brasil|brazil|\bBR\b|rio de janeiro|belo horizonte|curitiba|porto alegre|florian[oó]polis|recife|"
    r"campinas|bras[ií]lia|salvador|fortaleza|goi[aâ]nia|barueri|osasco|santo andr[eé]|s[aã]o jos[eé]|"
    r"\b(RJ|MG|PR|RS|SC|PE|DF|BA|CE|GO|ES)\b", re.I)
_LATAM = re.compile(r"latam|latin america|am[eé]rica latina|south america", I)
_COUNTRY_ONLY = re.compile(r"^\s*(brasil|brazil|br)\s*$", I)
_REMOTE_ONLY = re.compile(r"^\s*(100%\s*)?(remot[oe]|remote|home office)\s*$", I)

# --- Requisitos especiais
_YEARS = re.compile(r"(?:m[ií]nimo de |pelo menos |at least |mais de |\b)(\d{1,2})\s*\+?\s*(?:a \d+ )?(?:anos|years|yrs)", I)
_ENGLISH = re.compile(r"ingl[eê]s|english", I)
_ENGLISH_LEVEL = re.compile(
    r"fluen|avan[cç]ad|advanced|proficien|conversa|intermedi[aá]ri|upper|business|t[eé]cnico|technical|nativ", I)
_EDUCATION = re.compile(
    r"gradua[cç][aã]o|ensino superior|curso superior|bacharel|bachelor|degree|tecn[oó]logo|"
    r"forma[cç][aã]o (superior|em|acad)|ci[eê]ncia da computa|computer science|engenharia de", I)

_AFFIRMATIVE = re.compile(
    r"exclusiv[ao]s? (para|a) (pcd|pessoas com defici[eê]ncia|mulheres|pessoas negras|pessoas pretas|pessoas trans)|"
    r"(women|black people|pcd|pwd) applicants only|vaga afirmativa", I)

_EN_WORDS = re.compile(r"\b(the|and|you|with|we|our|will|for|experience)\b", I)
_PT_WORDS = re.compile(r"\b(de|e|voc[eê]|com|para|nossa?|experi[eê]ncia|que|uma?)\b", I)


@dataclass
class ParsedJob:
    description: str
    requirements_text: str
    desired_text: str
    required_skills: list[str] = field(default_factory=list)
    required_any_of: list[list[str]] = field(default_factory=list)  # grupos "X ou Y": basta um
    desired_skills: list[str] = field(default_factory=list)
    mentioned_skills: list[str] = field(default_factory=list)
    required_certs: list[str] = field(default_factory=list)
    desired_certs: list[str] = field(default_factory=list)
    seniority: str = "unspecified"
    modality: str = "UNKNOWN"
    location_class: str = "unknown"   # sp | brazil | brazil_any | latam | abroad | unknown
    years_required: int | None = None
    years_evidence: list[str] = field(default_factory=list)
    english: dict[str, Any] = field(default_factory=dict)  # {required, level, evidence, section}
    education_required: bool = False
    description_language: str = "pt"
    affirmative: str | None = None    # vaga afirmativa: trecho que indica o público
    sections_found: list[str] = field(default_factory=list)

    @property
    def technologies(self) -> list[str]:
        grouped = {s for g in self.required_any_of for s in g}
        return sorted(set(self.required_skills) | grouped | set(self.desired_skills) | set(self.mentioned_skills))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("description")
        d["technologies"] = self.technologies
        return d


class JobParser:
    def __init__(self, taxonomy: Taxonomy) -> None:
        self.tax = taxonomy

    def parse(self, raw: RawJob) -> ParsedJob:
        text = normalize_lines(raw.description_text) if raw.description_text else html_to_text(raw.description_html)
        sections = self._split_sections(text)

        req_text = "\n".join(sections["required"])
        des_text = "\n".join(sections["desired"])
        other_text = "\n".join(sections["other"] + sections["unknown"])

        required, groups = self._required_skills(sections["required"])
        all_required = required | {s for g in groups for s in g}
        desired = self.tax.find_skills(des_text) - all_required
        mentioned = self.tax.find_skills(other_text + "\n" + raw.title) - all_required - desired
        has_req_section = bool(sections["required"])

        lang = _language(text)
        parsed = ParsedJob(
            description=text,
            requirements_text=req_text,
            desired_text=des_text,
            required_skills=sorted(required),
            required_any_of=groups,
            desired_skills=sorted(desired),
            mentioned_skills=sorted(mentioned),
            required_certs=sorted(self.tax.find_certs(req_text if has_req_section else text)),
            desired_certs=sorted(self.tax.find_certs(des_text)),
            seniority=detect_seniority(raw.title, text),
            description_language=lang,
            sections_found=[k for k in ("required", "desired", "other") if sections[k]],
        )
        parsed.desired_certs = sorted(set(parsed.desired_certs) - set(parsed.required_certs))
        aff = _AFFIRMATIVE.search(raw.title) or _AFFIRMATIVE.search(text)
        parsed.affirmative = aff.group(0) if aff else None
        parsed.location_class = classify_location(raw.location, lang)
        parsed.modality = detect_modality(raw.modality_hint, raw.location, raw.title, text)
        self._special_requirements(parsed, sections, has_req_section)
        return parsed

    def _required_skills(self, lines: list[str]) -> tuple[set[str], list[list[str]]]:
        """Separa obrigatórias (todas) de grupos de alternativas ("Java, Go ou Rust" = basta uma)."""
        required: set[str] = set()
        groups: list[list[str]] = []
        for line in lines:
            found = self.tax.find_skills(line)
            if len(found) >= 2 and _ALTERNATIVES.search(line):
                groups.append(sorted(found))
            else:
                required |= found
        # grupo que contém algo já obrigatório por si só é redundante
        groups = [g for g in groups if not set(g) & required]
        return required, groups

    # ------------------------------------------------------------------ seções
    def _is_heading(self, line: str) -> bool:
        """Cabeçalho = marcado no HTML (##), termina com ':' ou é curto e sem tecnologia.
        Assim "Kubernetes é um diferencial" continua sendo conteúdo, não troca a seção."""
        if line.startswith(HEADING):
            return True
        if line.startswith(BULLET) or len(line) > 90:
            return False
        if line.endswith(":"):
            return True
        return len(line.split()) <= 5 and not line.endswith(".") and not self.tax.find_skills(line)

    def _split_sections(self, text: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {"required": [], "desired": [], "other": [], "unknown": []}
        current = "unknown"
        for line in text.split("\n"):
            if self._is_heading(line):
                if _DESIRED_HEAD.search(line):
                    current = "desired"
                    continue
                if _REQUIRED_HEAD.search(line):
                    current = "required"
                    continue
                if _OTHER_HEAD.search(line):
                    current = "other"
                    continue
            line = line.removeprefix(HEADING)
            target = current
            if current == "required" and _DESIRED_INLINE.search(line):
                target = "desired"
            out[target].append(line)
        return out

    # -------------------------------------------------- anos / inglês / formação
    def _special_requirements(self, p: ParsedJob, sections: dict[str, list[str]], has_req: bool) -> None:
        # Sem seção de requisitos identificada, o texto inteiro conta como potencial exigência.
        req_lines = sections["required"] if has_req else sections["unknown"] + sections["other"]
        years: list[int] = []
        for line in req_lines:
            if re.search(r"experi", line, I):
                for m in _YEARS.finditer(line):
                    n = int(m.group(1))
                    if 1 <= n <= 20:
                        years.append(n)
                        p.years_evidence.append(line[:200])
        p.years_required = max(years) if years else None

        for section, lines in (("required", req_lines), ("desired", sections["desired"])):
            for line in lines:
                if _ENGLISH.search(line) and not p.english:
                    level = _ENGLISH_LEVEL.search(line)
                    p.english = {"required": section == "required", "section": section,
                                 "level": level.group(0).lower() if level else None,
                                 "evidence": line[:200]}
        if not p.english and p.description_language == "en":
            p.english = {"required": True, "section": "inferred", "level": None,
                         "evidence": "Descrição da vaga redigida em inglês."}
        p.education_required = any(_EDUCATION.search(line) for line in req_lines)


def detect_seniority(title: str, text: str = "") -> str:
    for level, pattern in _SENIORITY:
        if pattern.search(title):
            return level
    for level, pattern in _ROMAN:
        if pattern.search(title):
            return level
    m = _SENIORITY_IN_TEXT.search(text)
    if m:
        word = m.group(2).lower()
        for level, pattern in _SENIORITY:
            if pattern.search(word):
                return level
    return "unspecified"


def detect_modality(hint: str | None, location: str | None, title: str, text: str) -> str:
    if hint:
        return hint
    for source in (location or "", title):
        if _HYBRID.search(source):
            return "HYBRID"
        if _REMOTE.search(source):
            return "REMOTE"
        if _ONSITE.search(source):
            return "ONSITE"
    if _HYBRID.search(text):
        return "HYBRID"
    if _REMOTE.search(text):
        return "REMOTE"
    if _ONSITE.search(text):
        return "ONSITE"
    return "UNKNOWN"


def classify_location(location: str | None, description_language: str = "pt") -> str:
    loc = location or ""
    if not loc.strip():
        return "unknown"
    if _SP.search(loc):
        return "sp"
    if _COUNTRY_ONLY.match(loc):
        return "brazil_any"   # só o país: cidade/modalidade indefinidas
    if _BR.search(loc):
        return "brazil"
    if _LATAM.search(loc):
        return "latam"
    if _REMOTE_ONLY.match(loc):
        return "brazil" if description_language == "pt" else "unknown"
    return "abroad"


def _language(text: str) -> str:
    sample = text[:4000]
    return "en" if len(_EN_WORDS.findall(sample)) > len(_PT_WORDS.findall(sample)) else "pt"
