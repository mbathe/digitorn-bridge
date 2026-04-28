---
id: app-as-mcp-server
---

# App-as-MCP-Server

> **Status: Not yet implemented.** This feature is planned for a future release, after stabilization of the multi-agent system.

A deployed Digitorn app can expose itself as an **MCP server**. The app's tools become accessible from any MCP client -- Claude Desktop, Cursor, Windsurf, or another Digitorn app.

## Motivation

### The Problem

An MCP server like Gmail exposes 19 raw tools: `search_emails`, `read_email`, `get_attachment`, `send_email`, `delete_email`, etc. The end user (or the agent consuming these tools) must understand each tool, its parameters, and orchestrate calls in the right order.

For a simple task like "summarize today's emails", the LLM must:

1. Call `search_emails` with the right parameters
2. Interpret the raw result
3. Call `read_email` for each email
4. Synthesize everything

That's **5+ LLM round-trips** -- slow, expensive, and fragile.

### The Solution

Encapsuler cette logique dans une **app Digitorn** qui expose des tools de haut niveau :

- `daily_digest` - resume des mails du jour (1 appel, l'agent interne fait les 5 etapes)
- `smart_search` - recherche en langage naturel (1 appel)
- `send_report` - compile un rapport Drive et l'envoie par email (1 appel)

L'app interne a un agent avec un system prompt specialise, la securite, la gestion de contexte, et les 19 tools Gmail. L'exterieur ne voit que 3 tools simples.

## Concept

### Architecture

```
                    Client MCP (Claude Desktop, Cursor, autre app Digitorn)
                    voit: smart_search, daily_digest, send_report
                              |
                              | Protocole MCP (stdio / SSE / HTTP)
                              v
              +------------------------------------------+
              |  App Digitorn: email-assistant (deployed) |
              |                                          |
              |  Agent "assistant" (Claude Sonnet)       |
              |  +-----------+ +-----------+             |
              |  | Gmail MCP | | Drive MCP |             |
              |  | 19 tools  | | 12 tools  |             |
              |  +-----------+ +-----------+             |
              |  + securite + contexte + approval        |
              +------------------------------------------+
```

### Bidirectionnel

Digitorn devient un **hub MCP bidirectionnel** :

- **Consomme** des serveurs MCP (Gmail, Slack, GitHub, etc.)
- **Expose** des apps comme serveurs MCP

Chaque app deployee enrichit l'ecosysteme. Plus il y a d'apps, plus il y a de tools MCP disponibles.

## Configuration YAML

### Minimal

```yaml
app:
  app_id: email-assistant
  name: "Smart Email Assistant"

modules:
  mcp:
    config:
      servers:
        - gmail
        - google_drive

agents:
  - id: assistant
    brain:
      provider: openrouter
      model: anthropic/claude-sonnet-4
      backend: openai_compat
      config:
        api_key: "{{secret.OPENROUTER_API_KEY}}"
        base_url: "https://openrouter.ai/api/v1"
    system_prompt: |
      Tu es un assistant email intelligent.
      Tu geres Gmail et Google Drive.

expose:
  as_mcp: true
  tools:
    - name: smart_search
      description: "Recherche intelligente dans les emails avec langage naturel"
      params:
        query: { type: string, description: "Recherche en langage naturel" }
        max_results: { type: integer, default: 10 }
      agent: assistant

    - name: daily_digest
      description: "Resume des emails importants du jour"
      params:
        categories:
          type: array
          items: { type: string }
          default: ["urgent", "work"]
      agent: assistant

    - name: send_report
      description: "Compile un rapport depuis Drive et l'envoie par email"
      params:
        drive_query: { type: string }
        recipient: { type: string }
        subject: { type: string }
      agent: assistant

execution:
  mode: one_shot
```
### Champs `expose:`

| Champ | Type | Defaut | Description |
|-------|------|--------|-------------|
| `as_mcp` | bool | `false` | Active l'exposition comme serveur MCP |
| `transport` | string | `"stdio"` | Transport MCP pour servir : `stdio`, `sse`, `streamable_http` |
| `port` | int | auto | Port pour SSE/HTTP (auto-assigne si omis) |
| `tools` | list | *required* | Liste des tools exposes |

### Champs par tool expose

| Champ | Type | Defaut | Description |
|-------|------|--------|-------------|
| `name` | string | *required* | Nom du tool MCP expose |
| `description` | string | *required* | Description pour les clients MCP |
| `params` | dict | `{}` | JSON Schema des parametres d'entree |
| `agent` | string | entry agent | Agent interne qui traite les requetes |
| `timeout` | float | `120.0` | Timeout par requete |

## App Composition

### App qui consomme une autre app

Une app Digitorn exposee en MCP peut etre consommee comme n'importe quel serveur MCP :

```yaml
# project-manager.yaml
app:
  app_id: project-manager
  name: "Project Manager"

modules:
  mcp:
    config:
      servers:
        - email-assistant    # une app Digitorn exposee !
        - jira-assistant     # une autre app Digitorn !
        - slack

agents:
  - id: pm
    brain:
      provider: openrouter
      model: anthropic/claude-sonnet-4
      backend: openai_compat
      config:
        api_key: "{{secret.OPENROUTER_API_KEY}}"
        base_url: "https://openrouter.ai/api/v1"
    system_prompt: |
      Tu es un chef de projet.
      Utilise email-assistant pour les mails,
      jira-assistant pour les tickets,
      et Slack pour les notifications.

execution:
  mode: conversation
```
Le chef de projet voit :
- `mcp_email_assistant.smart_search` - pas les 19 tools Gmail bruts
- `mcp_email_assistant.daily_digest`
- `mcp_jira_assistant.create_ticket`
- `mcp_slack.post_message`

### Hierarchie infinie

```
project-manager (app Digitorn)
  |-- email-assistant (app Digitorn exposee en MCP)
  |     |-- Gmail (serveur MCP)
  |     |-- Google Drive (serveur MCP)
  |-- jira-assistant (app Digitorn exposee en MCP)
  |     |-- Jira (serveur MCP natif)
  |-- Slack (serveur MCP natif)
```

Des agents qui utilisent des agents qui utilisent des serveurs MCP - le tout defini en YAML declaratif.

## Flow d'execution

### Quand un client MCP appelle un tool expose

```
1. Client MCP appelle smart_search(query="mails de LinkedIn")
       |
       v
2. Digitorn recoit la requete MCP (JSON-RPC tools/call)
       |
       v
3. Route vers l'agent interne "assistant"
       |
       v
4. L'agent execute un one_shot turn :
   - Recoit le system prompt + query en input
   - Appelle search_emails via Gmail MCP
   - Lit les resultats, filtre, formate
   - Retourne la reponse
       |
       v
5. Digitorn serialise la reponse en MCP Content
       |
       v
6. Client MCP recoit le resultat
```

### Avec flows (futur)

Quand les flows seront implementes, un tool expose pourra utiliser un **flow deterministe** au lieu de l'agent loop :

```yaml
expose:
  as_mcp: true
  tools:
    - name: daily_digest
      description: "Resume des emails du jour"
      flow:
        - id: search
          action: mcp_gmail.search_emails
          params:
            query: "after:{{today}} is:unread"
            max_results: 20

        - id: read_all
          map: "{{result.search}}"
          action: mcp_gmail.read_email
          params:
            message_id: "{{item.id}}"

        - id: summarize
          agent: assistant
          input: |
            Resume ces emails :
            {{result.read_all}}
```
Avantages du flow :
- **Deterministe** : les etapes 1-2 ne passent pas par le LLM
- **Rapide** : seule l'etape 3 (synthese) a besoin du LLM
- **Fiable** : pas de risque que le LLM oublie une etape

## Security

### Security profile interne

Le security profile de l'app interne s'applique toujours. Si `send_email` est en `approve` dans l'app, un appel externe qui declenche `send_email` sera aussi bloque en attente d'approbation.

```yaml
# email-assistant.yaml
capabilities:
  grant:
    - module: mcp_gmail
      actions: [search_emails, read_email, list_labels]
  approve:
    - module: mcp_gmail
      actions: [send_email, draft_email]
  deny:
    - module: mcp_gmail
      actions: [delete_email, batch_delete_emails]

expose:
  as_mcp: true
  tools:
    - name: send_report
      agent: assistant
      # Quand send_report declenche send_email en interne,
      # l'approbation est requise - meme depuis un client externe
```
### Isolation

Chaque requete MCP entrante est traitee dans une **session isolee**. Pas de fuite de contexte entre les clients.

## Comparaison avec l'existant

| Aspect | API REST (actuelle) | App-as-MCP-Server |
|--------|--------------------|--------------------|
| Protocole | HTTP REST | MCP (stdio/SSE/HTTP) |
| Clients | Custom SDK | Tout client MCP |
| Decouverte | `/tools/search` API | MCP `tools/list` natif |
| Interop | Digitorn-only | Ecosysteme entier |
| Composition | Via API calls | Via `servers:` YAML |

L'API REST et le MCP server coexistent - ce sont deux interfaces sur la meme app. L'API REST est pour les integrations custom (web apps, SDKs). Le MCP server est pour l'interop ecosysteme.

## Prerequisites d'implementation

Cette feature depend de composants qui ne sont pas encore implementes :

1. **Stabilite MCP client** - la normalisation des resultats, la connexion standalone, la securite bout-en-bout doivent etre testees en production
2. **Flows** ([07-flows.md](07-flows.md)) - pour les chemins deterministes dans les tools exposes
3. **Multi-agent** ([12-multi-agent.md](12-multi-agent.md)) - pour la delegation inter-agents

Ordre d'implementation prevu :

```
MCP client stable (en cours)
  -> Flows (orchestration deterministe)
    -> Multi-agent (delegation)
      -> App-as-MCP-Server (exposition)
```

## Positionnement

| Feature | LangChain | CrewAI | AutoGen | Digitorn |
|---------|-----------|--------|---------|----------|
| Consomme MCP | Via lib | Non | Non | Natif (catalogue + pool) |
| Expose MCP | Non | Non | Non | App-as-MCP-Server |
| Composition agents | Code Python | Code Python | Code Python | YAML declaratif |
| Securite bout-en-bout | Manuel | Non | Non | Natif (grant/approve/deny) |
| Deploiement | DIY | DIY | DIY | `digitorn app deploy` |

Digitorn serait le premier framework ou chaque app deployee enrichit l'ecosysteme MCP global.
