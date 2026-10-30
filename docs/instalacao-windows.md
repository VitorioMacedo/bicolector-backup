# Instalação no servidor Windows (sem Docker)

A aplicação roda como um serviço do Windows e grava num banco do SQL Server já existente na rede. Nada é
instalado além do Python (via `uv`), do driver ODBC e do próprio serviço.

## 1. O que pedir à equipe de infraestrutura / segurança

| Item | Para quê |
| --- | --- |
| Conta de serviço do Windows (local ou de domínio), sem privilégio de administrador | rodar o serviço `bic` |
| Banco `bic` no SQL Server + login da aplicação dono desse banco (`sql/mssql_create_db.sql`) | leituras, cadastro, agregados |
| Login somente leitura `powerbi` no mesmo banco (`docs/powerbi.md`, seção 1) | relatório do Power BI |
| Saída TCP do servidor para os 3 Link150 na porta 502 | Modbus TCP |
| Entrada TCP na porta 8000 (ou 443 com proxy, seção 7) a partir da rede dos usuários | painel web e API |
| Saída TCP para o SQL Server (1433 ou a porta da instância) | banco |
| Edição do SQL Server diferente de Express | o Express limita cada banco a 10 GB (poucos meses de leituras de 1 s) |

Espaço em disco no SQL Server: cerca de 20 leituras por segundo. Com compressão de página, estime 2 a 3 GB por
mês de leituras brutas, apagadas depois de `raw_retention_days` (padrão 90 dias), mais algumas centenas de MB
por ano de agregados, que nunca são apagados.

## 2. Pré-requisitos na máquina

1. [Microsoft ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)
   (instalador MSI, exige administrador). Confira em *Fontes de dados ODBC → Drivers*.
2. `uv` (gerenciador do Python): no PowerShell, `winget install astral-sh.uv` ou
   `irm https://astral.sh/uv/install.ps1 | iex`. Ele instala o Python 3.12 sozinho no primeiro `uv sync`.
   Sem acesso à internet no servidor: rode `uv sync` numa máquina com internet e copie a pasta inteira,
   incluindo `.venv`, ou use `uv python install 3.12` e `uv sync --offline` com um cache pré-carregado.
3. A pasta do projeto, por exemplo `C:\bic` (o `.venv` fica dentro dela).

## 3. Banco de dados

Como administrador do SQL Server (SSMS ou `sqlcmd`), rode `sql\mssql_create_db.sql` depois de trocar a senha:

```powershell
sqlcmd -S SERVIDOR\INSTANCIA -E -i C:\bic\sql\mssql_create_db.sql
```

O script cria o banco `bic`, o login `bic` e o torna dono do banco (a aplicação cria e altera tabelas, índices e
views na inicialização, idempotente). Nada mais precisa ser criado à mão: na primeira subida a aplicação aplica
`sql/mssql.sql`.

## 4. Configuração

```powershell
cd C:\bic
copy .env.example .env
notepad .env
```

- `DATABASE_URL`: `mssql+aioodbc://bic:SENHA@SERVIDOR:1433/bic?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes`.
  Instância nomeada: `SERVIDOR%5CINSTANCIA` no lugar de `SERVIDOR:1433` (`%5C` é a barra invertida). Se o SQL
  Server tiver certificado válido, remova `TrustServerCertificate=yes`. Caracteres especiais na senha precisam
  de URL-encoding (`@` → `%40`, `#` → `%23`).
- `ADMIN_USER` / `ADMIN_PASSWORD`: login do painel (página `/login`). Para não deixar a senha em texto puro, rode
  `uv run python -m app.auth.hashpw`, cole a linha `ADMIN_PASSWORD_HASH=...` gerada no `.env` e remova `ADMIN_PASSWORD`.
- `SECRET_KEY`: chave aleatória que assina o cookie de sessão (sem ela, todos precisam logar de novo a cada reinício).
- `COOKIE_SECURE=true` quando o painel estiver atrás de HTTPS (ver seção de proxy). Após 5 senhas erradas o IP fica
  bloqueado por 15 min (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_MINUTES`); as tentativas vão para o log.
- `API_KEY`: chave para integrações (Power Automate etc.), enviada no header `X-API-Key`.
- `MODBUS_TIMEOUT_S`: igual ou maior que o *serial timeout* do Link150 (padrão 1 s).

Teste antes de instalar o serviço:

```powershell
uv sync --no-dev
uv run --no-dev uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abra `http://localhost:8000/`, entre na página de login com o usuário e senha do `.env` e cadastre os gateways e medidores. O log deve mostrar
`esquema do banco verificado` e `coletor iniciado`. `Ctrl+C` para parar.

## 5. Serviço do Windows

Usamos o [WinSW](https://github.com/winsw/winsw) (um executável só, sem instalação), com a configuração pronta em
`deploy\bic-service.xml`:

1. Baixe `WinSW-x64.exe` da página de releases e salve como `C:\bic\deploy\bic-service.exe`.
2. Em `bic-service.xml`, confira `<workingdirectory>` (pasta do projeto) e `<executable>` (caminho do `uv.exe`:
   `where.exe uv` mostra; para a conta de serviço prefira um caminho fixo como `C:\bic\tools\uv.exe`, copiando
   o executável para lá).
3. PowerShell como administrador:

```powershell
cd C:\bic\deploy
.\bic-service.exe install
.\bic-service.exe start
.\bic-service.exe status
```

Para rodar com a conta de serviço, em *Serviços → BIC → Logon* informe a conta (ela precisa de leitura na pasta
e escrita em `deploy\logs`). Logs em `deploy\logs\bic-service.out.log`. O serviço reinicia sozinho se cair e
sobe com o Windows.

**Atualizar a aplicação**: `.\bic-service.exe stop`, copie a nova versão por cima (mantendo o `.env`),
`uv sync --no-dev`, `.\bic-service.exe start`. Mudanças de esquema são aplicadas na subida.

## 6. Firewall e rede

```powershell
New-NetFirewallRule -DisplayName "BIC painel" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow
```

Os gateways Link150 precisam responder na porta 502 a partir deste servidor (`Test-NetConnection IP -Port 502`).

## 7. HTTPS (opcional)

A aplicação fala HTTP simples. Para HTTPS, coloque um proxy na frente: IIS com *URL Rewrite* + *Application
Request Routing* apontando `https://bic.empresa/` → `http://localhost:8000/`, ou Caddy/nginx. Nesse caso libere
no firewall só a porta do proxy e deixe a 8000 restrita ao próprio servidor.

## 8. Power BI

Veja `docs/powerbi.md`: login somente leitura, On-premises Data Gateway (o conector SQL Server já vem com ele) e
o relatório `powerbi/bic.pbip`, cujo parâmetro `Servidor` recebe `SERVIDOR\INSTANCIA` ou `SERVIDOR,1433`.

## 9. Operação

- Backup: o banco `bic` entra na rotina normal de backup do SQL Server (full + log). Não há mais nada com estado
  fora dele além do `.env`.
- Retenção: `Configurações → Retenção das leituras brutas` no painel; a limpeza roda dentro da aplicação a cada
  10 min, em lotes de 20 mil linhas.
- Agregados (`agg_*`): recalculados pela aplicação a cada minuto. Se o serviço ficar parado, ao voltar ele
  recomeça de onde parou (`agg_state.watermark`); nada precisa ser rodado à mão.
- Saúde: `GET /healthz` responde `{"ok": true}`; o painel mostra fila do gravador e último erro de banco.
