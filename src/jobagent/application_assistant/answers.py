"""Sugestões de resposta para perguntas de formulário, só com fatos do profile.yaml.

Origem de cada resposta:
- profile:  montada direto de dados do perfil (links, formação, idiomas, preferências);
- template: "Você possui experiência com X?" — montada a partir da evidência derivada
            (projeto/certificação/declarado). Diz explicitamente quando NÃO há experiência;
- llm:      perguntas abertas ("Por que quer trabalhar conosco?"), validadas como o currículo;
- manual:   escrita por você.
Respostas que dependem de dado que só você tem (pretensão salarial, disponibilidade...) ficam
vazias com needs_input=True. O sistema nunca sugere valores.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from ..config import AppConfig, Settings, get_settings
from ..database import Answer, Job
from ..job_parser import JobParser, ParsedJob, Taxonomy
from ..profile import Profile, ProfileIndex
from ..resume_generator.generator import parse_job
from ..resume_generator.llm import LLMError, LLMProvider, credentials_available, get_provider
from ..resume_generator.prompts import profile_block
from ..resume_generator.selector import CATEGORIES, build_plan, job_skill_weights
from ..resume_generator.validator import ResumeValidator

log = logging.getLogger(__name__)
MAX_EXPERIENCE_QUESTIONS = 8


class AnswerDraft(BaseModel):
    answer: str = Field(description="Resposta em 1 a 2 parágrafos curtos, primeira pessoa, só com fatos do perfil e da vaga")


ANSWER_RULES = """\
Você redige respostas para perguntas de formulários de candidatura em nome de uma pessoa.
Use apenas fatos do perfil fornecido e informações que a própria descrição da vaga traz sobre a
empresa. Nada inventado: a resposta será enviada a um recrutador real e qualquer exagero a
prejudica em entrevista. Um validador automático rejeita tecnologias, termos, números e
certificações que não estão no perfil.

- Primeira pessoa, tom direto e cordial, 60 a 140 palavras, sem clichês ("apaixonado", "proativo").
- Para falar de si: projetos práticos, formação, certificações e conquistas do perfil. O perfil não tem
  experiência profissional registrada; não escreva "experiência profissional", "anos de experiência",
  "atuei como", "trabalhei", "em produção", nem se descreva como sênior/especialista.
- Formação em andamento aparece como em andamento. Itens "planned" só como planejados.
- Sobre a empresa, cite apenas o que está na descrição da vaga; se ela não diz nada sobre a empresa,
  foque no trabalho descrito na vaga.
- Responda no idioma da pergunta.
"""


@dataclass
class Suggestion:
    key: str
    question: str
    text: str
    source: str
    needs_input: bool = False


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " e " + items[-1]


_SALARY_Q = re.compile(r"pretens[aã]o|sal[aá]ri|remunera|salary|compensation|expectativa de ganho", re.I)
_EXPERIENCE_Q = re.compile(r"experi[eê]ncia|conhece|conhecimento|trabalhou com|j[aá] usou|experience|familiar", re.I)
_AVAILABILITY_Q = re.compile(r"disponibilidade para in[ií]cio|quando (pode|poderia) come[cç]ar|start date|notice period|aviso pr[eé]vio", re.I)
_ENGLISH_Q = re.compile(r"ingl[eê]s|english", re.I)
_MOTIVATION_Q = re.compile(r"por que|porque|why|motiva|interesse", re.I)


class AnswerService:
    def __init__(self, config: AppConfig, settings: Settings | None = None,
                 provider: LLMProvider | None = None) -> None:
        self.settings = settings or get_settings()
        self.tax = Taxonomy(config.taxonomy)
        self.parser = JobParser(self.tax)
        self.idx = ProfileIndex(Profile.from_dict(config.profile), self.tax)
        self.validator = ResumeValidator(self.idx)
        self._provider = provider

    # ================================================================ geração
    def suggest_all(self, job: Job, session: Session, use_llm: bool = True) -> list[Answer]:
        parsed = parse_job(job, self.parser)
        suggestions = self._standard(job, parsed, use_llm)
        existing = {a.key: a for a in self._answers(session, job)}
        for pos, sg in enumerate(suggestions):
            a = existing.get(sg.key)
            if a is None:
                a = Answer(job_id=job.id, key=sg.key, question=sg.question)
                session.add(a)
            # Nunca sobrescreve o que você já revisou: atualiza só a sugestão
            a.suggested, a.source, a.needs_input, a.position = sg.text, sg.source, sg.needs_input, pos
            if not a.reviewed:
                a.final_text = sg.text or None
        session.flush()
        if job.status == "READY_TO_APPLY":
            from .approval import current_approval, revoke
            if current_approval(session, job) is None:
                revoke(session, job, "respostas regeneradas")
        session.commit()
        return self._answers(session, job)

    def add_custom(self, job: Job, session: Session, question: str, use_llm: bool = True) -> Answer:
        parsed = parse_job(job, self.parser)
        sg = self._answer_question(job, parsed, question.strip(), use_llm)
        n = 1 + sum(1 for a in self._answers(session, job) if a.key.startswith("custom:"))
        a = Answer(job_id=job.id, key=f"custom:{n}", question=question.strip(), suggested=sg.text,
                   final_text=sg.text or None, source=sg.source, needs_input=sg.needs_input, position=1000 + n)
        session.add(a)
        session.commit()
        return a

    def save(self, answer: Answer, text: str, job: Job | None = None) -> list[str]:
        """Grava sua versão e marca como revisada. Avisos não bloqueiam: você é a autoridade."""
        changed = (answer.final_text or "").strip() != text.strip()
        answer.final_text = text.strip()
        answer.reviewed = bool(answer.final_text)
        if changed and job is not None:
            from .approval import revoke
            revoke(object_session(answer), job, f"resposta editada: {answer.question[:60]}")
        extra = ResumeValidator.numbers_in(job.description) if job else set()
        ignore = [job.title, job.company] if job else []
        answer.warnings = (self.validator.validate_text(answer.final_text, "resposta", extra, ignore)
                           if answer.final_text else [])
        if answer.warnings and answer.key.startswith("exp:"):
            # Respostas de experiência citam a tecnologia que você NÃO tem ("ainda não usei Datadog")
            answer.warnings = [w for w in answer.warnings if "não consta no perfil" not in w and "não aparece" not in w]
        return answer.warnings

    @staticmethod
    def _answers(session: Session, job: Job) -> list[Answer]:
        return list(session.scalars(select(Answer).where(Answer.job_id == job.id).order_by(Answer.position, Answer.id)))

    # ================================================================ conjunto padrão
    def _standard(self, job: Job, parsed: ParsedJob, use_llm: bool) -> list[Suggestion]:
        out = [self._motivation(job, parsed, use_llm), self._salary(), self._availability()]
        wm = self._work_model(job, parsed)
        if wm:
            out.append(wm)
        if parsed.english:
            out.append(self._english())
        out += self._experience_set(job, parsed)
        out.append(self._links())
        return out

    def _experience_set(self, job: Job, parsed: ParsedJob) -> list[Suggestion]:
        weights = job_skill_weights(parsed)
        job_literals = self.tax.find_skill_literals(job.description)
        # Uma pergunta por grupo de alternativas ("Java, Go ou Rust"), depois obrigatórias, depois desejáveis
        seen: set[str] = set()
        out: list[Suggestion] = []
        for group in parsed.required_any_of:
            terms = [self._job_term(s, job_literals) for s in group]
            out.append(Suggestion(f"exp:{'|'.join(group)}", f"Você possui experiência com {_join_or(terms)}?",
                                  self._experience_group_text(group, job_literals), "template"))
            seen.update(group)
        for sid, _w in sorted(weights.items(), key=lambda kv: -kv[1]):
            if sid in seen or len(out) >= MAX_EXPERIENCE_QUESTIONS:
                continue
            seen.add(sid)
            term = self._job_term(sid, job_literals)
            out.append(Suggestion(f"exp:{sid}", f"Você possui experiência com {term}?",
                                  self.experience_text(sid, term), "template"))
        return out

    # ================================================================ respostas individuais
    def _job_term(self, sid: str, job_literals: dict[str, set[str]]) -> str:
        terms = sorted(job_literals.get(sid) or {self.tax.name(sid)}, key=len)
        return terms[0]

    def experience_text(self, sid: str, term: str) -> str:
        idx = self.idx
        covering = idx.covers(sid)
        no_prof = not idx.has_professional_experience
        if not covering:
            related = self._related(sid)
            text = f"Ainda não tenho experiência com {term}."
            if related:
                text += f" Tenho experiência prática relacionada com {related}."
            return text + " Tenho interesse em aprender e aplicar."
        direct = covering[0] == sid
        exact = self._is_exact(sid, term)
        parts = []
        for i, cid in enumerate(covering[:2]):
            ev = idx.evidence[cid]
            name = idx.display_name(cid)
            projects = [s.removeprefix("projeto ") for s in ev.sources if s.startswith("projeto ")]
            certs = [s.removeprefix("certificação ") for s in ev.sources if s.startswith("certificação ")]
            if projects:
                detail = self._highlight(projects[0], cid) if i == 0 else ""
                parts.append(f"Utilizei {name} em projeto prático ({_join(projects)})." + (f" {detail}" if detail else ""))
            if certs:
                parts.append(f"Possuo a certificação {_join(certs)}.")
            if not projects and not certs:
                parts.append(f"Tenho conhecimentos em {name}" + (" pela formação." if "academic" in ev.kinds else "."))
        if not exact:
            return f"Não especificamente com {term}, mas tenho base relacionada. " + " ".join(parts)
        lead = "Sim. " if direct else f"Sim, por meio de {_join([idx.display_name(c) for c in covering[:2]])}. "
        text = lead + " ".join(parts)
        if no_prof:
            text += f" Ainda não tenho experiência profissional com {term}."
        return text

    def _is_exact(self, sid: str, term: str) -> bool:
        """O termo exato da vaga é verdadeiro para você? "RHEL" não é, mesmo você tendo Linux."""
        covering = self.idx.covers(sid)
        if not covering:
            return False
        if covering[0] == sid:
            return self.idx.literal_allowed(sid, term)
        return term.lower() in self.tax.canonical_terms(sid)

    def _experience_group_text(self, group: list[str], job_literals: dict[str, set[str]]) -> str:
        owned = [s for s in group if self.idx.covers(s)]
        if not owned:
            terms = _join_or([self._job_term(s, job_literals) for s in group])
            return self.experience_text(group[0], terms)
        return " ".join(self.experience_text(s, self._job_term(s, job_literals)) for s in owned[:2])

    def _highlight(self, project_name: str, sid: str) -> str:
        pr = next((p for p in self.idx.profile.projects if p.name == project_name), None)
        if not pr:
            return ""
        for h in pr.highlights:
            if sid in self.tax.find_skills(h) and len(h.split()) >= 4:
                return f"No {pr.name}: {h[0].lower() + h[1:]}"
        return ""

    def _related(self, sid: str) -> str:
        """Skills suas da mesma categoria, com projeto (ex.: Datadog -> OpenTelemetry e Jaeger)."""
        idx = self.idx
        for ids in CATEGORIES.values():
            if sid in ids:
                rel = [s for s in ids if s != sid and s in idx.owned_skills and "project" in idx.evidence[s].kinds][:3]
                if rel:
                    projects = sorted({src.removeprefix("projeto ") for s in rel for src in idx.evidence[s].sources
                                       if src.startswith("projeto ")})
                    return _join([idx.display_name(s) for s in rel]) + (f" ({_join(projects)})" if projects else "")
        return ""

    def _salary(self) -> Suggestion:
        sal = self.idx.profile.application_preferences.salary
        q = "Qual sua pretensão salarial?"
        parts = []
        if sal.clt_monthly:
            parts.append(f"R$ {_brl(sal.clt_monthly)} mensais (CLT)")
        if sal.pj_monthly:
            parts.append(f"R$ {_brl(sal.pj_monthly)} mensais (PJ)")
        if not parts:
            return Suggestion("salary", q, "", "profile", needs_input=True)
        text = "Minha pretensão é de " + " ou ".join(parts) + "."
        if sal.note:
            text += f" {sal.note.rstrip('.')}."
        return Suggestion("salary", q, text, "profile")

    def _availability(self) -> Suggestion:
        av = self.idx.profile.application_preferences.availability
        q = "Qual sua disponibilidade para início?"
        return Suggestion("availability", q, f"Disponibilidade {av}." if av else "", "profile", needs_input=not av)

    def _work_model(self, job: Job, parsed: ParsedJob) -> Suggestion | None:
        prefs = self.idx.profile.application_preferences
        mod = (job.modality or "UNKNOWN").lower()
        labels = {"remote": "remoto", "hybrid": "híbrido", "onsite": "presencial"}
        if mod not in labels:
            return None
        where = f" em {job.location}" if job.location and mod != "remote" else ""
        q = f"Você tem disponibilidade para o modelo {labels[mod]}{where}?"
        loc_ok = mod == "remote" or any(_norm(l.split("/")[0]) in _norm(job.location or "") for l in prefs.locations)
        if mod in prefs.work_models and loc_ok:
            return Suggestion("work_model", q, f"Sim, tenho disponibilidade para o modelo {labels[mod]}{where}.", "profile")
        if mod in prefs.work_models and prefs.relocation is True:
            return Suggestion("work_model", q, f"Sim. Tenho disponibilidade para o modelo {labels[mod]} e para mudança de cidade.", "profile")
        return Suggestion("work_model", q, "", "profile", needs_input=True)

    def _english(self) -> Suggestion:
        langs = [l for l in self.idx.profile.languages if re.search(r"ingl|english", l.name, re.I)]
        q = "Qual seu nível de inglês?"
        if not langs:
            return Suggestion("english", q, "", "profile", needs_input=True)
        return Suggestion("english", q, f"Inglês {langs[0].level}." if langs[0].level else "Inglês.", "profile")

    def _links(self) -> Suggestion:
        links = self.idx.profile.personal.links
        text = " · ".join(f"{k.capitalize()}: {v}" for k, v in links.items() if v)
        return Suggestion("links", "LinkedIn / GitHub / Portfólio", text, "profile", needs_input=not text)

    # ================================================================ perguntas abertas
    def _answer_question(self, job: Job, parsed: ParsedJob, question: str, use_llm: bool) -> Suggestion:
        if _SALARY_Q.search(question):
            sg = self._salary()
        elif _AVAILABILITY_Q.search(question):
            sg = self._availability()
        elif _ENGLISH_Q.search(question) and re.search(r"n[ií]vel|level|fala|speak", question, re.I):
            sg = self._english()
        elif _EXPERIENCE_Q.search(question) and (found := self.tax.find_skill_literals(question)):
            sid, terms = next(iter(found.items()))
            sg = Suggestion("", question, self.experience_text(sid, sorted(terms)[0]), "template")
        else:
            sg = self._llm_answer(job, parsed, question, use_llm) or Suggestion("", question, "", "manual", True)
        sg.question = question
        return sg

    def _motivation(self, job: Job, parsed: ParsedJob, use_llm: bool) -> Suggestion:
        q = "Por que você quer trabalhar conosco?"
        sg = self._llm_answer(job, parsed, q, use_llm)
        if sg:
            sg.key = "motivation"
            return sg
        return Suggestion("motivation", q, self.motivation_template(job, parsed), "template")

    def motivation_template(self, job: Job, parsed: ParsedJob) -> str:
        idx = self.idx
        plan = build_plan(idx, parsed)
        # Descreve a VAGA com os termos dela (ex.: "TCP/IP"), só entre o que você cobre
        job_literals = self.tax.find_skill_literals(job.description)
        matched = list(dict.fromkeys(
            t for s in plan.matched_skills
            if self._is_exact(s, t := self._job_term(s, job_literals))))[:4]
        top = plan.projects[0] if plan.projects else None
        proj = idx.project(top.project_id) if top else None
        text = f"Tenho interesse na vaga de {job.title} na {job.company} porque o trabalho descrito"
        text += f" envolve {_join(matched)}, temas em que venho me desenvolvendo." if matched else " está alinhado com a minha formação."
        if proj and top.matched:
            text += f" No projeto {proj.name}, utilizei {_join([idx.display_name(s) for s in top.matched[:4]])}."
        grad = next((e for e in idx.profile.education if e.level == "graduacao"), None)
        post = next((e for e in idx.profile.education if e.level == "pos_graduacao"), None)
        if grad:
            text += f" Sou formado em {grad.degree} ({grad.institution})"
            text += f" e estou cursando a {post.degree}." if post and post.status == "in_progress" else "."
        text += " Quero aplicar e aprofundar esses conhecimentos em um time que trabalha com isso no dia a dia."
        return text

    def _llm_answer(self, job: Job, parsed: ParsedJob, question: str, use_llm: bool) -> Suggestion | None:
        if not use_llm or not (self._provider or credentials_available(self.settings)):
            return None
        provider = self._provider or get_provider(self.settings)
        system = [ANSWER_RULES, profile_block(self.idx)]
        extra_numbers = ResumeValidator.numbers_in(job.description)
        violations: list[str] = []
        for _ in range(self.settings.llm_max_attempts):
            user = (f"VAGA: {job.title} — {job.company}\n\nDESCRIÇÃO DA VAGA:\n{job.description}\n\n"
                    f"PERGUNTA DO FORMULÁRIO: {question}")
            if violations:
                user += "\n\nA versão anterior foi REJEITADA pelo validador. Corrija:\n" + "\n".join(f"- {v}" for v in violations)
            try:
                res = provider.generate(system, user, output_format=AnswerDraft)
            except LLMError as e:
                log.warning("LLM falhou na resposta: %s", e)
                return None
            text = res.draft.answer.strip()
            violations = self.validator.validate_text(text, "resposta", extra_numbers, [job.title, job.company])
            if not violations:
                return Suggestion("", question, text, "llm")
        log.info("resposta do LLM reprovada no validador: %s", violations)
        return None


def _join_or(items: list[str]) -> str:
    items = list(dict.fromkeys(items))
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " ou " + items[-1]


def _brl(value: int) -> str:
    return f"{value:,}".replace(",", ".")
