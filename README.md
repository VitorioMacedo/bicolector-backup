# bic — coletor de energia PM2230

Lê 20 medidores Schneider EasyLogic PM2230 através de gateways Link150/EGX150 (Modbus TCP → RTU),
grava em **SQL Server** e alimenta dashboards Power BI. Inclui painel web de administração,
API REST e reset remoto da energia acumulada.

## Como rodar

Desenvolvimento (SQL Server 2022 Developer em container + aplicação):

```bash
cp .env.example .env            # ajuste senhas e chave da API
docker compose up -d --build    # banco + aplicação em http://localhost:8000
```

Produção: a aplicação roda como serviço do Windows contra o SQL Server do cliente, sem Docker. Passo a passo em
[`docs/instalacao-windows.md`](docs/instalacao-windows.md) (driver ODBC 18, `sql/mssql_create_db.sql`, WinSW).

Depois, no painel (página de login com `ADMIN_USER`/`ADMIN_PASSWORD` do `.env`): cadastre os gateways (IP de cada Link150) e os medidores
(nome da máquina + unit id) pelos botões "novo gateway" / "novo medidor". O coletor começa a ler no
próximo ciclo, sem reiniciar.

## Painel web

- **Painel** (`/`): KPIs, um card por gateway com o tempo de ciclo e um mini-gráfico dos últimos ciclos,
  tabela ao vivo dos medidores (busca por máquina, filtro por gateway e por status). Clicar numa máquina
  abre o detalhe com todas as grandezas e os botões de reset (energia, demanda, mín/máx).
- **Gráficos** (`/charts`): dois gráficos com o mesmo período (15m a 30d ou de/até). Em cima, o **ranking de
  consumo**: kWh de todos os medidores em colunas, do maior para o menor, com o valor sempre visível, cada
  coluna na cor do gateway e o consumo acumulado por gateway nos cartões acima. Embaixo, o **gráfico por
  grandeza**: qualquer grandeza medida (potência, corrente, tensão, FP, energia, demanda, frequência, THD),
  até 12 medidores em tons da cor do gateway (do 7º do mesmo gateway em diante, tracejado), resolução (automática, 1 s a 1 dia), tipo (linhas, área, barras,
  degraus) e CSV. Clicar numa coluna do ranking coloca o medidor em foco no gráfico de baixo; clicar num
  gateway seleciona todos os seus medidores.
- **Gateways e medidores** (`/meters`): cadastro e edição em modais, salvando pela API JSON sem recarregar.
- **Configurações** e **Comandos** (histórico dos resets).

Identidade visual: laranja / preto / branco (`app/web/static/app.css`). Para usar o logo da empresa, salve
o arquivo em `app/web/static/logo.png`; ele aparece no cabeçalho sem reiniciar. Cada gateway tem uma **cor**
(campo no cadastro, sugerida de uma paleta acessível) que identifica seus medidores nos gráficos. Não há build de front:
o JavaScript é vanilla (módulos ES) e a biblioteca de gráficos (uPlot 1.6.32) está vendorizada em
`app/web/static/vendor/`.

Sem hardware, suba os simuladores dos 3 gateways reais (10 / 8 / 2 medidores, 80 ms por requisição):

```bash
docker compose --profile sim up -d
# no painel: gateways host=sim-gw1 porta 5020 (units 1-10), sim-gw2:5021 (1-8), sim-gw3:5022 (1-2)
```

## Desenvolvimento local

```bash
uv sync                                     # instala Python 3.12 + dependências
uv run pytest                               # testes unitários e de integração com o simulador
uv run python -m tests.sim_server --port 5020 --units 1-10 --latency-ms 80
uv run uvicorn app.main:app --reload        # precisa de um SQL Server em DATABASE_URL (e do ODBC Driver 18)
BIC_TEST_DATABASE_URL=... uv run pytest     # inclui o teste do SQL real (rollups, histórico, gravador, retenção)
```

## Regras de operação que importam

- **Timeout**: `MODBUS_TIMEOUT_S` deve ser maior ou igual ao *serial timeout* configurado no Link150
  (padrão de fábrica 1 s). Menor que isso, a resposta atrasada de um medidor ausente chega fora de
  ordem; o cliente detecta e recria a conexão, mas o ciclo sofre. Um medidor ausente custa um timeout
  por tentativa até ser marcado offline (`offline_after_failures`) e entrar em backoff de 5 s a 60 s.
- **Gateway mais carregado**: no gateway de 10 medidores o bloco `full` (94 registradores) usa quase
  todo o segundo e não sobra tempo para o ciclo lento. O bloco `compact` (76 registradores, só médias
  de corrente e tensão) libera ~10 % do ciclo e permite uma leitura lenta por ciclo. Troque pelo painel.
- **Intervalo**: o PM2230 atualiza valores 1x por segundo; abaixo de 1 s só se repetem leituras.
  O painel mostra "ciclo X ms / intervalo Y ms" e avisa quando o intervalo não é alcançável.
- **Agregados (`agg_*`)**: são tabelas de resumo recalculadas pela própria aplicação a cada minuto
  (`app/db/rollup.py`): o bucket anterior é refeito (leituras atrasadas) e o bucket em andamento é gravado
  parcial (o Power BI vê o dia/hora corrente). `agg_state.watermark` marca o início do bucket em andamento; as
  consultas do painel leem a tabela de resumo antes dele e as tabelas brutas a partir dele, então o gráfico é
  sempre atual. `agg_fast_1min` (todas as colunas rápidas por minuto) e `agg_slow_1h` (lentas por hora)
  alimentam as resoluções de 1 min a 1 dia (as maiores são rollups na hora, média de médias). Se a aplicação
  ficar parada, ao voltar ela recomeça do watermark; nada precisa ser rodado à mão.
- **Retenção e duplicatas**: a limpeza das leituras brutas (`raw_retention_days`) e dos ciclos (30 dias) roda
  na aplicação a cada 10 min, em lotes. As chaves primárias de `reading_*` têm `IGNORE_DUP_KEY = ON`: um lote
  reenviado depois de uma queda de conexão não derruba a gravação.
- **Horários**: tudo é gravado em UTC (`DATETIME2`); as views `v_pbi_*` convertem para o fuso de
  *Configurações → Fuso horário* (nome IANA, traduzido para o nome de fuso do Windows que o SQL Server usa).

## API dos gráficos

- `GET /api/history?metric=p_total&meter_ids=1,2&range=6h` (ou `from=...&to=...` em ISO 8601;
  `bucket=auto|1s|5s|30s|1min|5min|15min|1h|1d`). Uma grandeza por chamada (`GET /api/metrics` lista as 37:
  colunas de `reading_fast`/`reading_slow` mais `kwh`, o consumo por bucket). Resolução automática pelo
  período: até 6 h lê as leituras brutas, acima usa `agg_fast_1min`/`agg_slow_1h` (grandezas lentas: bucket
  mínimo de 1 min; `kwh`: 5 min). Resposta: `t` (epoch s, UTC) + `series[meter_id]` com `name`, `gateway_id`
  e `v` (média por bucket, `null` sem amostra). Até 50 000 pontos por série quando o bucket é forçado.
  O último bucket (em andamento) vem sempre das tabelas brutas, mesmo nas resoluções agregadas.
- `GET /api/energy/ranking?range=24h`: kWh consumido no período por todos os medidores, em ordem decrescente,
  e o total por gateway (com a cor). O consumo é a diferença do acumulador entre buckets; se o acumulador
  cai (reset), o bucket conta o valor pós-reset; se ele salta para cima (medidor trocado), o salto conta
  como consumo.

## Benchmark em campo (etapa de levantamento)

```bash
uv run python -m tools.benchmark --host <ip do Link150> --units 1-10 --rounds 20 --slow
```

Mostra a latência real por medidor e diz se o gateway alcança o intervalo de 1 s.

## Estrutura

- `app/modbus/` — mapa de registradores do PM2230, cliente por gateway, comandos (reset)
- `app/poller/` — agendador (1 task por gateway, ciclos rápido e lento), gravação em lote
- `app/api/`, `app/web/` — API REST e painel web (templates Jinja2 + `static/*.js` vanilla, sem build)
- `app/db/rollup.py` — tabelas de resumo (`agg_*`), watermark e retenção (tarefa de fundo junto do coletor)
- `sql/mssql.sql` — índices, compressão, tabelas de agregados e views para o Power BI (aplicado na subida)
- `sql/mssql_create_db.sql` — criação do banco e do login da aplicação (roda uma vez, como administrador)
- `deploy/` — serviço do Windows (WinSW) para o servidor do cliente
- `tools/benchmark.py` — medição de campo
- `tools/pbip_gen.py` — gera o projeto Power BI `powerbi/bic.pbip` (modelo TMDL em DirectQuery + relatório PBIR)
- `powerbi/` — relatório Power BI pronto: consumo, potência, FP, corrente e tensão por medidor, lendo as views `v_pbi_*`
- `docs/powerbi.md` — como abrir o relatório, conectar o Power BI (DirectQuery) e o botão de reset
