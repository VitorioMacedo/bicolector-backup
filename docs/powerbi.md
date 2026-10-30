# Power BI: conectando ao banco do coletor

## 1. Usuário somente leitura no SQL Server

Execute uma vez, como administrador do SQL Server (troque a senha; ou use um login do Windows/AD do time de BI):

```sql
USE bic;
CREATE LOGIN powerbi WITH PASSWORD = 'troque-esta-senha', CHECK_POLICY = OFF;
CREATE USER powerbi FOR LOGIN powerbi;
ALTER ROLE db_datareader ADD MEMBER powerbi;
```

## 2. On-premises Data Gateway

O Power BI Service só alcança um banco na rede do cliente através do **On-premises Data Gateway**
da Microsoft. Instale-o numa máquina Windows da mesma rede (pode ser o próprio servidor do coletor),
entre com a conta do Power BI e cadastre a fonte **SQL Server** apontando para `SERVIDOR\INSTANCIA` (ou
`servidor,1433`), banco `bic`, autenticação Básica com o usuário `powerbi` (ou Windows). O conector do SQL
Server já vem no gateway; nenhum driver extra é necessário.

## 3. Relatório pronto: `powerbi/bic.pbip`

O projeto `powerbi/` já traz o modelo (DirectQuery no SQL Server) e o relatório com as mesmas informações da
página **Gráficos** do painel web, tema laranja/preto/branco.

1. Abra `powerbi/bic.pbip` no Power BI Desktop (versão de 2025 ou mais nova, formato PBIR/TMDL).
2. Na primeira abertura ele pede as credenciais do SQL Server: usuário `powerbi` (seção 1) ou, no
   ambiente local do docker compose, `bic` / `BicDev2026x` (autenticação *Banco de dados*). Se o servidor não
   tiver certificado confiável (caso do SQL Server em container, autoassinado), desmarque *Criptografar conexão*
   na tela de credenciais; se já salvou com a opção marcada, todos os visuais ficam em erro: corrija em
   *Transformar dados → Configurações da fonte de dados → Editar Permissões* (desmarque a criptografia).
3. Servidor e banco são **parâmetros** do modelo (`Servidor` = `localhost`, `Banco` = `bic`): troque em
   *Transformar dados → Editar parâmetros* para `SERVIDOR\INSTANCIA` ou `servidor,1433`, sem regerar.
4. Se aparecer "um ou mais objetos calculados precisam ser atualizados" ou o aviso de relações, clique em
   *Atualizar agora* (as tabelas Período, Grandeza e Resolução são calculadas) e salve.

**Página "Gráficos"** (espelho da página web):

- **Período**: botões 15 min / 1 h / 6 h / 24 h / 7 dias / 30 dias (padrão 24 h), contados a partir de agora,
  ou o segmentador *de / até* por dia. Quando um intervalo de dias é escolhido, o botão de período é ignorado.
- **Consumo por medidor**: cartão com o total de kWh, **pizza de kWh por gateway** (soma do consumo dos
  medidores de cada gateway, na cor cadastrada do gateway), cartões por gateway (kWh, nº de medidores ativos,
  % do total) e colunas de kWh por medidor em ordem decrescente, coloridas pela cor do gateway, com o valor
  escrito em cada coluna. Clicar numa coluna ou numa fatia filtra o gráfico por grandeza (o "foco" da web).
  O kWh do período usa a mesma fonte do ranking do painel: buckets de 5 min até 7 dias, de 1 h até 90 dias,
  de 1 dia acima disso.
- **Gráfico por grandeza**: seletores de *Grandeza* (consumo kWh, kW, kvar, kVA, FP, corrente, tensão L-N,
  tensão L-L, demanda de pico, frequência, energia acumulada), *Resolução* (1 min, 5 min, 15 min, 1 h, 1 dia),
  *Gateways* e *Medidores*; uma linha por medidor com rótulos de dados. Só os **8 maiores consumidores** do
  período entre os medidores selecionados são desenhados (medida `Posição no ranking`, mesmo limite da web). Nem toda
  grandeza existe em toda resolução (o subtítulo do gráfico lista as combinações): consumo kWh só em 5 min /
  1 h / 1 dia, demanda em 1 h / 1 dia, frequência em 1 h, tensão L-L em 1 min / 5 min.
- Diferenças em relação à web: o tipo do gráfico é sempre linha (o Power BI não troca o tipo por seletor), as
  cores das linhas seguem a paleta do tema (não o tom por gateway) e a resolução não é automática.

**Página "Agora"**: cartões de kW total, kWh acumulado e medidores com leitura, e a tabela com a última
leitura de cada máquina.

**Atualização automática.** Em DirectQuery cada clique ou filtro já consulta o banco, mas os visuais só se
renovam sozinhos com a *atualização automática de página*, que o projeto já traz ligada a cada 10 s nas duas
páginas (formato do visual → *Atualização de página*). No Power BI Desktop qualquer intervalo vale. No Power
BI Service o limite depende do workspace: Pro/compartilhado força no mínimo **30 minutos**; capacidade
Fabric/Premium (ou PPU) permite até 1 s, respeitando o mínimo que o administrador da capacidade definir. Para
"ao vivo" de verdade sem capacidade dedicada, use o painel web da aplicação (2 s) ou abra o relatório no Desktop.

Páginas e views:

| Página | Tabelas (views) | Conteúdo |
| --- | --- | --- |
| Gráficos | `v_pbi_1min`, `v_pbi_5min`, `v_pbi_15min`, `v_pbi_1h`, `v_pbi_1d` (Leituras 1 min … 1 dia) | consumo por medidor e por gateway no período; gráfico por grandeza e resolução |
| Agora | `v_pbi_agora` | última leitura de cada máquina e cartões de kW total / kWh acumulado |

Modelo: `Medidores` (`v_meter`, com a cor do gateway) e `Calendário` (`v_pbi_calendario`, um dia por linha)
são as dimensões; as tabelas `Leituras …` (uma por resolução, com grandezas elétricas **e** energia na mesma
linha) se relacionam por `meter_id` e `dia_local`. `Período`, `Grandeza` e `Resolução` são tabelas calculadas:
as duas últimas são *field parameters* (o seletor troca a medida e o eixo do gráfico); todas as medidas ficam
em `Medidores` e limitam o tempo por `bucket_utc >= agora − período`. As views `v_pbi_*` já devolvem o horário
local (`bucket_local`, fuso da configuração *display_timezone* do painel) e o consumo por bucket (`kwh` =
diferença do acumulador entre buckets consecutivos; num reset de energia o bucket mostra o valor pós-reset).
Só medidores ainda cadastrados aparecem.

O projeto é gerado por `tools/pbip_gen.py` (`uv run python -m tools.pbip_gen`), que recria as pastas de
tabelas e páginas a cada execução; os ids são determinísticos, então um `git diff` mostra só o que mudou.
Mude o gerador e regere em vez de editar o projeto à mão. Para publicar no Power BI Service use *Publicar* e
configure o gateway on-premises (seção 2) com a mesma fonte SQL Server.

## 4. Conexão manual no Power BI Desktop (se quiser montar outros visuais)

Obter dados → **Banco de dados SQL Server** → servidor `SERVIDOR\INSTANCIA`, banco `bic` → modo **DirectQuery**.
Selecione apenas as views/agregados abaixo. Nunca use `reading_fast` diretamente num visual de longo
período: são 20 linhas por segundo.

| Objeto | Granularidade | Uso sugerido |
| --- | --- | --- |
| `v_meter` | – | dimensão máquina / gateway (`gateway_color` = cor do gateway cadastrada no painel, `#rrggbb`) |
| `v_meter_latest` | última leitura | cartões "agora": kW, A, V, FP, kWh por máquina |
| `agg_1min` | 1 minuto | gráfico de potência das últimas horas (tabela de resumo, atualizada a cada minuto) |
| `agg_15min` | 15 minutos | potência e corrente do dia |
| `agg_1h` | 1 hora | perfil diário/semanal |
| `agg_1d` | 1 dia | histórico mensal/anual |
| `agg_energy_1h` | 1 hora | kWh consumidos por hora (`kwh_consumed`), demanda de pico, THD |
| `agg_energy_1d` | 1 dia | kWh por dia / mês (`kwh_consumed`) |
| `agg_fast_1min`, `agg_slow_1h` | 1 min / 1 h | média de **todas** as grandezas rápidas / lentas (usados pelo gráfico por grandeza do painel) |
| `agg_state` | – | watermark de cada tabela de resumo (diagnóstico: `updated_at` deve ser recente) |
| `v_energy_since_reset` | – | kWh desde o último reset de cada máquina |
| `v_poll_health` | 1 minuto | saúde do coletor (duração dos ciclos por gateway) |
| `v_pbi_1min`, `v_pbi_15min` | 1 min (7 dias) / 15 min (90 dias) | agregados já em horário local (`bucket_local`, `dia_local`), só medidores ativos |
| `v_pbi_5min` | 5 min (7 dias) | `kwh` consumido por bucket (último `kwh_import` do bucket em `reading_slow`) + médias de `agg_1min` |
| `v_pbi_1h`, `v_pbi_1d` | 1 h / 1 dia | grandezas elétricas e energia na mesma linha: `kwh`, `kwh_acumulado`, `demand_peak` (e `freq_avg`, `thd_*` em 1 h) |
| `v_pbi_energy_1h`, `v_pbi_energy_1d` | 1 h / 1 dia | `kwh` consumido por bucket (diferença do acumulador), demanda de pico |
| `v_pbi_calendario` | 1 dia | dimensão de datas para o segmentador de período |
| `v_pbi_agora` | última leitura | o mesmo que `v_meter_latest`, em horário local |

Relacione tudo por `meter_id` com `v_meter`. Em `agg_*` e `reading_*` o campo de tempo é `bucket`/`ts` em
**UTC** (`DATETIME2`); prefira as views `v_pbi_*`, que já convertem para o fuso configurado no painel
(*Configurações → Fuso horário*; o SQL Server usa o nome de fuso do Windows equivalente, gravado em
`app_setting.display_timezone_windows`). O bucket em andamento (minuto/hora/dia corrente) já aparece nas
tabelas de resumo, recalculado a cada minuto.

Dica de desempenho em DirectQuery: cada visual deve filtrar um intervalo de tempo e usar o agregado
compatível com o intervalo (horas → `agg_1min`; dias → `agg_15min`/`agg_1h`; meses → `agg_1d`).

## 5. Páginas sugeridas (além das três do projeto pronto)

1. **Visão geral** — cartões por máquina com `v_meter_latest` (p_total, i_avg, vln_avg, pf_total, kwh_import).
2. **Consumo** — barras de `agg_energy_1d.kwh_consumed` por máquina e por dia; ranking do mês.
3. **Potência e demanda** — linhas de `agg_1min.p_avg` por máquina; `agg_energy_1h.demand_peak`.
4. **Qualidade de energia** — `agg_1h.vln_min/vln_max`, `pf_min`, `agg_energy_1h.thd_*` com faixas de alerta.
5. **Detalhe da máquina** — todas as grandezas de uma máquina selecionada, drill-down até `agg_1min`.

## 6. Botão de reset dentro do Power BI (opcional)

O Power BI não escreve em dispositivos. O que ele pode fazer é chamar a API desta aplicação:

- Visual **Power Automate** no relatório → fluxo com ação **HTTP**: `POST http://<servidor>:8000/api/meters/<id>/reset-energy`
  com header `X-API-Key: <chave do .env>`. O fluxo roda na nuvem Microsoft, então a API precisa ser
  alcançável a partir dela (gateway on-premises para HTTP, VPN ou publicação com HTTPS e IP restrito).
- Alternativa sem exposição externa: o painel web da aplicação (`http://<servidor>:8000/`) já tem o
  botão "zerar kWh" com confirmação e registra quem executou.

O histórico fica em `command_log` (também exposto em `/api/commands`).
