# Job Application Agent

Sistema pessoal para encontrar vagas aderentes ao seu perfil, analisá-las e (nas próximas etapas)
preparar currículo e candidatura, sempre com a sua aprovação. Não é um bot de spam.
Arquitetura completa: [ARCHITECTURE.md](ARCHITECTURE.md).

**Status:** etapas 1 a 9: descoberta, análise, score, currículo adaptado com ATS, dashboard e fluxo de aprovação.

## Instalação (Windows / PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env      # modo padrão: DRY_RUN
jobagent init
```

## Antes de usar: revise o perfil

`config/profile.yaml` é a fonte única da verdade. Ele tem seções separadas para dados pessoais,
formação, certificações, conquistas, experiência, projetos (com `stack`, `highlights` e `planned`),
skills e idiomas. Rode `jobagent profile` para validar e ver a evidência derivada de cada skill.

### Itens a completar

Complete os campos `TODO` de `config/profile.yaml` (o `jobagent profile` lista quais faltam):
- `languages`: enquanto estiver vazio, toda vaga que exige inglês aparece como RISK
- `personal.email` / `phone`: só vão para o currículo final, nunca para o LLM
- `context` e `link` de cada projeto (acadêmico ou pessoal; URL do repositório)

Depois de editar o perfil, rode `jobagent rescore`.

## Uso

```powershell
jobagent sources                       # fontes configuradas
jobagent discover --preview            # coleta e mostra sem salvar
jobagent discover                      # coleta, analisa e salva (~13 requisições, ~40s)
jobagent discover --company xp         # só um board
jobagent list -m 75                    # match ≥ 75
jobagent list -s ANALYZED -l "São Paulo" -d 30 -t devops
jobagent show 10                       # MATCH / GAPS / RISKS + composição do score
jobagent show 10 --description
jobagent status 10 APPROVED --note "boa vaga"
jobagent import minhas_vagas.yaml      # vagas copiadas do LinkedIn etc. (formato: tests/fixtures/manual_jobs.yaml)
jobagent rescore                       # após editar profile/scoring/taxonomia
jobagent profile                       # valida o perfil e mostra a evidência de cada skill
jobagent resume 10                     # currículo adaptado + ATS (Claude, se houver chave)
jobagent resume 10 --no-llm            # só reorganiza o perfil, sem chamar API
```

## Currículo adaptado (etapa 7)

Configure no `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
JOBAGENT_LLM_MODEL=claude-sonnet-5-5     # trocável
JOBAGENT_LLM_EFFORT=high
```

`jobagent resume <id>` cria `applications/<empresa-cargo>/` com:

| Arquivo | Conteúdo |
|---|---|
| `job-description.md` | vaga completa |
| `analysis.md` | score, MATCH/GAPS/RISKS, **ATS Match: XX%**, como melhorar, "Por que este currículo foi adaptado desta forma?", validação |
| `resume.md` / `resume.pdf` | currículo (uma página, mesmo conteúdo) |
| `application.json` | metadados, ATS, rascunho estruturado, validação, modelo usado |

Como o currículo é montado:
1. Um **seletor determinístico** escolhe os projetos pela stack da vaga (por exemplo, AWS + Kubernetes + Terraform leva ao ToggleMaster).
2. O **Claude** redige o resumo e os bullets dentro dessa seleção. Skills, projetos e certificações são referenciados por ID, então não há como ele inventar nomes.
3. Um **validador** rejeita qualquer tecnologia, termo, número, certificação ou experiência que não esteja no `profile.yaml`. Isso inclui tecnologia de outro projeto, "Kafka" quando o perfil só tem SQS, "anos de experiência", "em produção" e funcionalidade planejada descrita como pronta. Quando há violação, o Claude recebe a lista e tenta de novo. Se falhar outra vez, o sistema usa a versão determinística.
4. O **ATS** é determinístico e compara os termos literais da vaga. As ausências aparecem separadas em "você possui, adicione" e "você não possui, não adicione".

Sem chave de API, tudo funciona em modo determinístico.

## Dashboard (etapa 8)

```powershell
jobagent dashboard            # abre http://127.0.0.1:8765/ no navegador (Ctrl+C para sair)
jobagent dashboard --port 9000 --no-open
```

- **Lista:** Vaga | Empresa | Match | Status | Currículo | Link, com contadores por status, filtros
  (match, status, empresa, cargo, localização, modalidade, data de publicação), ordenação e paginação.
- **Detalhe da vaga:** MATCH/GAPS/RISKS, composição do score, requisitos extraídos, histórico de status,
  mudança de status com nota, notas livres, botões "Gerar com Claude" e "Gerar sem LLM", e links para
  PDF, análise/ATS, markdown e json.
- **Regras de status:** "Pronta p/ candidatura" só pelo fluxo de aprovação (etapa 9). "Entrevista" e
  "Proposta" exigem "Candidatura enviada" antes. "Candidatura enviada" é só um registro: o sistema não envia nada.
- **Segurança:** o servidor escuta só em 127.0.0.1 e aceita apenas Host local. Ações (POST) só são
  aceitas vindas do próprio dashboard, e os arquivos servidos ficam restritos a `applications/`.

Para adicionar empresas, inclua o token do board em `config/sources.yaml`. O token aparece na URL
de vagas da empresa: `job-boards.greenhouse.io/<token>` ou `jobs.lever.co/<token>`.

## Aprovação de candidatura (etapa 9)

No dashboard, na vaga, use **Revisar e aprovar candidatura →** (`/jobs/<id>/apply`). Pela CLI:
`jobagent answers <id>` gera as sugestões e `jobagent review <id>` mostra o checklist.

**Respostas sugeridas**, sempre só com o seu `profile.yaml`:

| Pergunta | Origem |
|---|---|
| Por que quer trabalhar conosco? | Claude (validado como o currículo) ou template |
| Você possui experiência com X? (uma por tecnologia da vaga) | evidência do perfil: "Sim, no ToggleMaster…", "Não especificamente com RHEL, mas tenho Linux", "Ainda não tenho experiência com Datadog; tenho OpenTelemetry e Jaeger (ToggleMaster)" |
| Pretensão salarial, disponibilidade, inglês, mudança | `application_preferences` / `languages`. **Vazio = você escreve.** O sistema nunca sugere valores |
| Modelo de trabalho | suas preferências (remoto, híbrido em SP) |
| Perguntas do formulário que você colar | classificadas: salário/experiência/disponibilidade, ou Claude |

Você edita e confirma cada resposta. Avisos do validador sobre o seu texto aparecem, mas não bloqueiam.

**Aprovar** exige currículo validado, todas as respostas revisadas e a sua confirmação de que revisou
o currículo e leu os riscos. A aprovação grava um snapshot (hash do PDF + respostas) e gera
`answers.md`. **Editar uma resposta ou regenerar o currículo revoga a aprovação.**

Depois de aprovar, o dashboard mostra **Abrir formulário de candidatura ↗**, o PDF e botões "Copiar".
**Você envia no site da empresa** e então marca "Candidatura enviada" (com confirmação).
"Pronta" e "Enviada" não podem ser marcadas pelo seletor de status nem pela CLI sem aprovação.

## Testes

```powershell
pytest -q
```

Os testes rodam sem acesso à rede (HTTP mockado).
