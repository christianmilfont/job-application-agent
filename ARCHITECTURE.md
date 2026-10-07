# Arquitetura — Job Application Agent

Sistema **pessoal** de descoberta, análise e preparação de candidaturas.
Princípios inegociáveis:

1. **Qualidade > volume.** Nada de disparo em massa. O sistema filtra e prioriza.
2. **Verdade.** Tudo que o sistema afirma sobre você vem de `config/profile.yaml`. Nada é inventado.
3. **Humano no controle.** Nenhuma ação irreversível sem sua aprovação explícita.
4. **Respeito às plataformas.** Só APIs públicas/documentadas ou importação manual. Sem scraping de
   plataformas que o proíbem, sem contornar CAPTCHA, login, rate limit ou bloqueios.
5. **Determinístico primeiro, LLM depois.** Parser e score são regras explicáveis e testáveis; o LLM
   entra para redação (currículo, respostas), sempre restrito aos fatos do perfil.

---

## 1. Visão geral

```
                ┌───────────────────────── config/ ─────────────────────────┐
                │ profile.yaml  sources.yaml  skills_taxonomy.yaml  scoring │
                └──────┬───────────────┬──────────────────┬────────────┬────┘
                       │               │                  │            │
 ┌──────────────┐  ┌───▼──────────┐ ┌──▼─────────┐  ┌─────▼──────┐ ┌───▼──────────────┐
 │ job_collector│─▶│  job_parser  │─▶│ job_scorer │─▶│  database  │◀│ dashboard (8)    │
 │ greenhouse   │  │ seções, skills│ │ score+MATCH│  │ SQLite/PG  │ │ FastAPI + HTMX   │
 │ lever        │  │ senioridade  │ │ GAPS/RISKS │  │ dedup keys │ └───▲──────────────┘
 │ manual import│  │ modalidade   │ └────────────┘  └─────┬──────┘     │ aprovação
 └──────▲───────┘  └──────────────┘                       │            │
        │ PoliteClient (rate limit, robots, budget)        │      ┌─────┴──────────────┐
        │                                                  ├─────▶│ resume_generator(7)│
   APIs públicas                                           │      │ resume.md/pdf, ATS │
                                                           │      └────────────────────┘
                                                           │      ┌────────────────────┐
                                                           └─────▶│ application_       │
                                                                  │ assistant (9-10)   │
                                                                  │ respostas, guard,  │
                                                                  │ abre o form p/ você│
                                                                  └────────────────────┘
```

Pipeline DISCOVERY (implementado):
`collect → dedup → parse → filtro de relevância → score → salvar (FOUND → ANALYZED)`

## 2. Stack

| Camada | Escolha | Motivo |
|---|---|---|
| Linguagem | Python 3.11+ | ecossistema de scraping/LLM/PDF |
| HTTP | httpx | timeouts, transport mockável nos testes |
| Coleta | APIs JSON públicas (Greenhouse, Lever) | estáveis, permitidas, 1 requisição por empresa; **Playwright não é necessário para descoberta** |
| Banco | SQLAlchemy 2 + SQLite (padrão) | zero setup; troca para PostgreSQL por `JOBAGENT_DATABASE_URL` |
| Config | pydantic-settings + `.env` + YAML | segredos fora do código; regras editáveis sem programar |
| CLI | Typer + Rich | uso diário antes do dashboard |
| Dashboard (etapa 8) | FastAPI + Jinja2 + HTMX (vendorizado) | local, sem build de front-end, funciona offline |
| Currículo (etapa 7) | Markdown + ReportLab (PDF) a partir do mesmo modelo | sem dependências nativas no Windows; aparência consistente |
| LLM (etapa 7+) | Anthropic Claude (Sonnet 5.5 por padrão, configurável), structured outputs | ver §7 |
| Navegador (etapa 10) | Playwright **headed** (janela visível) | só para abrir/preencher o form com você olhando; o clique final é seu |

## 3. Estrutura de pastas

```
job-application-agent/
├── config/                    # YAMLs editáveis (perfil, fontes, taxonomia, pesos)
├── src/jobagent/
│   ├── config/                # Settings (.env) e loader dos YAMLs
│   ├── database/              # models, sessão, repositório, chaves de dedup
│   ├── job_collector/         # PoliteClient + coletores (greenhouse, lever, manual)
│   ├── job_parser/            # HTML→texto, seções, taxonomia, senioridade, modalidade
│   ├── job_scorer/            # score 0-100, MATCH/GAPS/RISKS, filtro de relevância
│   ├── pipeline/              # orquestração (discovery, rescore)
│   ├── profile/               # modelo do profile.yaml + evidência derivada
│   ├── resume_generator/      # seletor, LLM, validador, ATS, render md/pdf
│   ├── application_assistant/ # (etapas 9-10) guard de envio, respostas, abertura do form
│   ├── dashboard/             # FastAPI + templates + static (htmx, css)
│   ├── logging_setup.py       # logs com redação de segredos
│   └── cli.py                 # comando `jobagent`
├── applications/<empresa-cargo>/   # (etapa 6/7) job-description.md, analysis.md, resume.*, application.json
├── data/ logs/                # gerados localmente, fora do git
└── tests/                     # pytest, 100% offline (HTTP mockado)
```

## 4. Modelo de dados

**jobs**: dados da vaga (empresa, cargo, local, modalidade, senioridade, publicação, descrição limpa,
link, tipo de candidatura, recrutador), `parsed` (JSON do parser), `analysis` (score, MATCH/GAPS/RISKS,
breakdown), `status`, `raw` (payload original para auditoria).

**Deduplicação** — três chaves, todas `UNIQUE`; se qualquer uma bater, a vaga é ignorada:

| Chave | Como |
|---|---|
| `url_key` | URL normalizada (sem `utm_*`, `gh_src`, `trk`..., sem `www`, sem barra final) |
| `source_key` | `fonte:id_externo` (ex.: `greenhouse:4731894005`) |
| `content_key` | SHA-256 de `empresa|cargo|local` normalizados (sem acento/caixa/pontuação) |

**status_history**: toda transição de status com data e nota.
**collector_runs**: cada execução com estatísticas (auditoria de volume).
Etapas futuras adicionam `applications` (currículo gerado, respostas, aprovação, data de envio).

Status: `FOUND → ANALYZED → APPROVED → RESUME_GENERATED → READY_TO_APPLY → APPLIED → INTERVIEW/OFFER/REJECTED`.

## 5. Coleta (etapa 4) e revisão de conformidade

| Fonte | Como | Situação |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` — API pública documentada para exibir vagas | ✅ ativo |
| Lever | `api.lever.co/v0/postings/{token}?mode=json` — API pública documentada | ✅ ativo |
| Manual | `jobagent import arquivo.yaml` | ✅ ativo — caminho para LinkedIn, Gupy, indicações |
| LinkedIn | — | ❌ **não automatizado**: o User Agreement proíbe scraping/bots e automação de conta |
| Gupy | — | ❌ sem API pública documentada de busca; endpoint interno do portal não é usado |
| Workday, SmartRecruiters, Ashby | APIs/feeds públicos | possível extensão futura, mesma regra |

`PoliteClient` (todas as requisições passam por ele):
- intervalo mínimo por host (3s + jitter) e respeito a `Crawl-delay`;
- `robots.txt` verificado antes de cada host;
- orçamento de requisições por execução (padrão 100; uma descoberta completa usa ~13);
- `429/5xx` → backoff respeitando `Retry-After`;
- **`401/403` → para aquele coletor imediatamente.** Nunca troca IP, user-agent ou tenta de novo.
- User-Agent honesto identificando uso pessoal.

## 6. Parser e scoring (etapas 5-6)

Parser (determinístico): HTML → linhas com cabeçalhos/bullets → seções
(obrigatório / desejável / outros) → tecnologias via taxonomia (aliases + regex),
grupos de alternativas ("Java, Go ou Rust" = basta um), senioridade (título primeiro),
modalidade (dado estruturado > local > título > texto), anos de experiência, inglês, formação,
certificações, idioma da descrição, vaga afirmativa.

Score (pesos em `config/scoring.yaml`):

| Critério | Peso |
|---|---|
| Requisitos obrigatórios cobertos | 35 |
| Desejáveis cobertos | 10 |
| Cargo alinhado | 15 |
| Senioridade | 15 |
| Localização + modalidade | 15 |
| Formação + certificações | 10 |
| Penalidades | Sênior −20, Lead/Staff −30, ≥3 anos −10, 2 anos −4, inglês exigido não informado −3 |

Faixas: 90+ excelente · 75-89 alta · 60-74 possível · <60 não priorizar.
Desejável ausente → **GAP** (nunca elimina). Obrigatório ausente → **RISK** (reduz, não elimina).
Skills implícitas são mostradas como "via X" (ex.: SQL via PostgreSQL), nunca como experiência direta.

## 7. Currículo e ATS (etapa 7) — implementado

```
profile.yaml ─▶ ProfileIndex (evidência por skill: projeto / certificação / declarado)
                     │
vaga ─▶ parser ─▶ selector (projetos, skills, certificações por peso da vaga)
                     │
                     ├─▶ Claude (structured output ResumeDraft, IDs + texto livre)  ──┐
                     │        ▲ violações (1 nova tentativa)                          │
                     │        └──────────── validator ◀───────────────────────────────┘
                     └─▶ rascunho determinístico (fallback / --no-llm)
                                   │
                     render (md + pdf de uma página) ─▶ ATS ─▶ applications/<empresa-cargo>/
```

- **Provider/modelo** via `.env`: `JOBAGENT_LLM_PROVIDER=anthropic`, `JOBAGENT_LLM_MODEL=claude-sonnet-5-5`,
  `JOBAGENT_LLM_EFFORT`. A chamada usa `client.beta.messages.parse` com `output_format=ResumeDraft`, o
  system prompt (regras + perfil) com cache e `fallbacks: "default"` para recusas.
- **Dados enviados ao LLM:** perfil sem e-mail/telefone e a descrição da vaga. Nome e contato só
  entram no arquivo final, localmente.
- **Evidência derivada:** uma skill citada no stack de um projeto ganha "projeto X". Itens `planned`
  nunca contam como evidência.
- **Rótulos honestos:** quando a taxonomia lista exemplos ("Mensageria (Kafka/RabbitMQ/SQS)"), o
  currículo mostra só o que está no perfil ("Mensageria (SQS)").
- **Validador:** IDs existentes, tecnologia do próprio projeto em cada bullet, termo literal presente no
  perfil (exceto sinônimos marcados com `synonyms: true` na taxonomia), números do perfil, certificações
  possuídas, frases proibidas sem experiência profissional, formação em andamento e itens planejados.
- **ATS:** percentual determinístico sobre os termos literais da vaga (tecnologias, certificações,
  cargo, formação). Alternativas ("Java ou Go") contam uma vez. Ausências são separadas em adicionáveis
  e não adicionáveis.

## 8. Dashboard (etapa 8) — implementado

FastAPI + Jinja2 + HTMX (servido localmente em `/static`, sem CDN). `jobagent dashboard` sobe o Uvicorn
em `127.0.0.1:8765`.

| Rota | Função |
|---|---|
| `GET /` | lista com filtros (match, status, empresa, cargo, local, modalidade, dias), ordenação e paginação; com HTMX devolve só a tabela |
| `GET /jobs/{id}` | detalhe: MATCH/GAPS/RISKS, breakdown, requisitos, histórico, currículos |
| `POST /jobs/{id}/status` | transição de status com nota (regras em `application_assistant/workflow.py`) |
| `POST /jobs/{id}/resume` | gera currículo (com ou sem LLM) e dispara `statusChanged` |
| `POST /jobs/{id}/notes` | notas livres |
| `GET /resumes/{rid}/{arquivo}` | PDF inline; `.md` renderizado (HTML do conteúdo desativado); só os 5 arquivos do pacote |

Proteções de servidor local:
- `TrustedHostMiddleware` aceita só `127.0.0.1` e `localhost`, contra DNS rebinding.
- Todo POST exige o cabeçalho `HX-Request` (que força preflight CORS, não concedido) e `Origin` local,
  contra CSRF de sites abertos no mesmo navegador.
- Arquivos são resolvidos e só são servidos se estiverem dentro de `applications/`.
- Sem `/docs` nem OpenAPI.

## 9. Respostas e aprovação — implementado

`application_assistant/answers.py`, `approval.py`, `workflow.py`, `guard.py` e as tabelas `answers` e `approvals`.

- **Respostas** com origem rastreável (`profile`, `template`, `llm`, `manual`):
  - Experiência com X vem da evidência derivada (projeto, certificação, declarado). Se o termo exato
    da vaga não está no perfil (RHEL quando você tem Linux, Kafka quando você tem SQS), a resposta
    começa com "Não especificamente com…".
  - Perguntas abertas usam o Claude com o mesmo validador do currículo; se reprovado, usa template.
  - Dados que só você tem (salário, disponibilidade, inglês, mudança) ficam vazios com `needs_input`.
- **Snapshot da aprovação:** sha256 de {resume_id, sha256 do PDF, respostas finais}. `current_approval()`
  recalcula e só considera válida a aprovação idêntica ao estado atual. Regenerar o currículo, editar ou
  trocar resposta ou remover pergunta revoga automaticamente (volta para RESUME_GENERATED).
- **Guard:** `assert_submission_allowed(mode, job, user_confirmed, approval)` exige ASSISTED,
  READY_TO_APPLY, aprovação vigente e confirmação. A etapa 10 passa por ele.
- **Transições manuais:** READY_TO_APPLY só pela aprovação; APPLIED só com aprovação vigente e sua confirmação.

## 10. Candidatura assistida — desenho e revisão de segurança

Fluxo: vaga com score alto → currículo gerado → respostas sugeridas → **você revisa e edita** →
`APPROVED` → `READY_TO_APPLY` → o sistema **abre a página de candidatura** para você.

- `application_assistant.guard.assert_submission_allowed` (já implementado e testado) bloqueia qualquer
  envio fora do modo `ASSISTED`, sem status `READY_TO_APPLY` e sem confirmação explícita.
- Respostas ("Por que quer trabalhar conosco?", pretensão salarial, "Tem experiência com X?") são
  geradas só com fatos do perfil; pretensão salarial **nunca** é inventada — vem de um campo que você
  preenche, ou fica em branco para você decidir.
- Integração (etapa 10): Playwright em janela visível pode **preencher** campos de formulários de ATS
  (Greenhouse/Lever) com o que você aprovou. **O clique em "Enviar" é sempre seu.** Se houver CAPTCHA,
  login ou verificação, o sistema para e devolve o controle — não tenta resolver nem contornar.
- LinkedIn Easy Apply: não será automatizado (termos proíbem). O sistema prepara o material e abre o link.
- Sessões/cookies, se um dia forem necessários, ficam fora do projeto e do git, nunca em texto em logs.

## 11. Segurança

- Segredos só em `.env` (fora do git; `.env.example` documenta). Não existem campos de senha do LinkedIn.
- Logs com filtro de redação (Authorization, cookie, password, token, api_key, valores de segredos).
- Banco e `applications/` ficam locais e fora do git.
- Dashboard escutará apenas em localhost.

## 12. Modos

| Modo | Coleta/análise | Gera currículo | Abre/preenche form | Envia |
|---|---|---|---|---|
| `DRY_RUN` (padrão) | ✅ (grava só no banco local) | ✅ | simulado | ❌ |
| `DISCOVERY` | ✅ | ❌ | ❌ | ❌ |
| `ASSISTED` | ✅ | ✅ | ✅ com você olhando | só após sua confirmação; o clique final é seu |

`--preview` em `discover`/`import` não grava nada, nem localmente.
