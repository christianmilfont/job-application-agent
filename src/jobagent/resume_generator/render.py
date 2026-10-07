"""Renderização do currículo em Markdown e PDF a partir do mesmo modelo (ResumeDocument).
Nomes de skills, projetos, certificações e formação vêm do profile.yaml, nunca do LLM."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from ..profile import ProfileIndex
from .schema import ResumeDraft

_LABELS = {
    "pt": {"summary": "Resumo", "skills": "Competências", "projects": "Projetos", "certs": "Certificações",
           "education": "Formação", "achievements": "Conquistas", "languages": "Idiomas",
           "completed": "Concluído", "in_progress": "Em andamento", "training": "programa de formação",
           "proj_in_progress": "em evolução", "personal": "pessoal"},
    "en": {"summary": "Summary", "skills": "Skills", "projects": "Projects", "certs": "Certifications",
           "education": "Education", "achievements": "Achievements", "languages": "Languages",
           "completed": "Completed", "in_progress": "In progress", "training": "training program",
           "proj_in_progress": "in progress", "personal": "personal"},
}


@dataclass
class RProject:
    name: str
    tag: str
    stack: str
    bullets: list[str]
    link: str = ""


@dataclass
class ResumeDocument:
    lang: str
    name: str
    headline: str
    contact: list[str]
    summary: str
    skill_groups: list[tuple[str, list[str]]]
    projects: list[RProject]
    certifications: list[str]
    education: list[str]
    achievements: list[str]
    languages: list[str] = field(default_factory=list)

    @property
    def labels(self) -> dict[str, str]:
        return _LABELS.get(self.lang, _LABELS["pt"])


def build_document(draft: ResumeDraft, idx: ProfileIndex, lang: str) -> ResumeDocument:
    p = idx.profile
    L = _LABELS.get(lang, _LABELS["pt"])
    contact = [x for x in (p.personal.location, p.personal.email, p.personal.phone) if x]
    contact += [v.removeprefix("https://") for v in p.personal.links.values() if v]

    projects = []
    for pe in draft.projects:
        pr = idx.project(pe.project_id)
        if pr is None:
            continue
        tags = [t for t in (L["personal"] if pr.context == "pessoal" else pr.context,
                            L["proj_in_progress"] if pr.status == "in_progress" else "") if t]
        projects.append(RProject(pr.name, " · ".join(tags), " · ".join(pr.stack), pe.bullets, pr.link))

    certs = []
    for cid in draft.certification_ids:
        c = idx.certification(cid)
        if c:
            certs.append(f"{c.name} — {c.issuer}" + (f" ({L['training']})" if c.kind == "training" else ""))

    education = [f"{e.degree} — {e.institution} ({L[e.status]})" for e in p.education]
    groups = [(g.name, [idx.display_name(s) for s in g.skill_ids]) for g in draft.skill_groups if g.skill_ids]
    return ResumeDocument(
        lang=lang, name=p.personal.name or "(nome não informado)", headline=draft.headline, contact=contact,
        summary=draft.summary, skill_groups=groups, projects=projects, certifications=certs,
        education=education, achievements=[a.text for a in p.achievements],
        languages=[f"{lang_.name}" + (f" — {lang_.level}" if lang_.level else "") for lang_ in p.languages],
    )


# ----------------------------------------------------------------------------- Markdown
def render_markdown(doc: ResumeDocument) -> str:
    L = doc.labels
    out = [f"# {doc.name}", f"**{doc.headline}**", "", " · ".join(doc.contact), "",
           f"## {L['summary']}", doc.summary, "", f"## {L['skills']}"]
    out += [f"- **{name}:** {', '.join(items)}" for name, items in doc.skill_groups]
    out += ["", f"## {L['projects']}"]
    for pr in doc.projects:
        tag = f" _({pr.tag})_" if pr.tag else ""
        out += [f"### {pr.name}{tag}", f"_{pr.stack}_"]
        out += [f"- {b}" for b in pr.bullets]
        if pr.link:
            out.append(f"- {pr.link}")
        out.append("")
    out += [f"## {L['certs']}"] + [f"- {c}" for c in doc.certifications]
    out += ["", f"## {L['education']}"] + [f"- {e}" for e in doc.education]
    if doc.achievements:
        out += ["", f"## {L['achievements']}"] + [f"- {a}" for a in doc.achievements]
    if doc.languages:
        out += ["", f"## {L['languages']}"] + [f"- {lang}" for lang in doc.languages]
    return "\n".join(out).strip() + "\n"


# ----------------------------------------------------------------------------- PDF
def _fonts() -> tuple[str, str]:
    """Calibri/Arial do Windows quando disponíveis (melhor tipografia); senão Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    fonts_dir = Path("C:/Windows/Fonts")
    for regular, bold in (("calibri.ttf", "calibrib.ttf"), ("arial.ttf", "arialbd.ttf")):
        r, b = fonts_dir / regular, fonts_dir / bold
        if r.exists() and b.exists():
            try:
                pdfmetrics.registerFont(TTFont("Body", str(r)))
                pdfmetrics.registerFont(TTFont("Body-Bold", str(b)))
                return "Body", "Body-Bold"
            except Exception:  # noqa: BLE001 — fonte corrompida: cai para Helvetica
                pass
    return "Helvetica", "Helvetica-Bold"


def render_pdf(doc: ResumeDocument, path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

    regular, bold = _fonts()
    accent = colors.HexColor("#1F3A5F")
    muted = colors.HexColor("#555555")
    base = ParagraphStyle("base", fontName=regular, fontSize=9.6, leading=12.6, alignment=TA_LEFT)
    st = {
        "name": ParagraphStyle("name", parent=base, fontName=bold, fontSize=20, leading=24, textColor=accent),
        "headline": ParagraphStyle("headline", parent=base, fontSize=11.5, leading=15, textColor=accent),
        "contact": ParagraphStyle("contact", parent=base, fontSize=9, textColor=muted),
        "h2": ParagraphStyle("h2", parent=base, fontName=bold, fontSize=11.5, leading=14, textColor=accent,
                             spaceBefore=7, spaceAfter=1),
        "h3": ParagraphStyle("h3", parent=base, fontName=bold, fontSize=10.5, spaceBefore=4),
        "stack": ParagraphStyle("stack", parent=base, fontSize=8.8, leading=11.5, textColor=muted),
        "body": base,
    }

    def section(title: str) -> list:
        return [Paragraph(escape(title.upper()), st["h2"]),
                HRFlowable(width="100%", thickness=0.6, color=accent, spaceBefore=0, spaceAfter=4)]

    def bullets(items: list[str]) -> ListFlowable:
        return ListFlowable([ListItem(Paragraph(escape(i), base), leftIndent=10, value="•") for i in items],
                            bulletType="bullet", start="•", leftIndent=10, bulletFontSize=8)

    L = doc.labels
    story: list = [Paragraph(escape(doc.name), st["name"]), Paragraph(escape(doc.headline), st["headline"]),
                   Spacer(1, 2), Paragraph(escape("  ·  ".join(doc.contact)), st["contact"])]
    story += section(L["summary"]) + [Paragraph(escape(doc.summary), base)]
    story += section(L["skills"])
    for name, items in doc.skill_groups:
        story.append(Paragraph(f"<font name='{bold}'>{escape(name)}:</font> {escape(', '.join(items))}", base))
    story += section(L["projects"])
    for pr in doc.projects:
        title = escape(pr.name) + (f" <font color='#555555' size='9'>({escape(pr.tag)})</font>" if pr.tag else "")
        block = [Paragraph(title, st["h3"]), Paragraph(escape(pr.stack), st["stack"]), bullets(pr.bullets)]
        if pr.link:
            block.append(Paragraph(escape(pr.link), st["stack"]))
        story.append(KeepTogether(block))
    story += section(L["certs"]) + [bullets(doc.certifications)]
    story += section(L["education"]) + [bullets(doc.education)]
    if doc.achievements:
        story += section(L["achievements"]) + [bullets(doc.achievements)]
    if doc.languages:
        story += section(L["languages"]) + [bullets(doc.languages)]

    pdf = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=13 * mm, bottomMargin=13 * mm, title=f"{doc.name} — {doc.headline}",
                            author=doc.name)
    pdf.build(story)
