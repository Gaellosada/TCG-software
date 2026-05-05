# %% [markdown]
# <!-- SECTION:1:strategy_description -->
# # 1. Strategy Description
#
# {{strategy_description_md}}

# %% [markdown]
# <!-- SECTION:2:assumptions -->
# # 2. Assumptions
#
# {{assumptions_table_md}}

# %% [markdown]
# <!-- SECTION:3:data_summary -->
# # 3. Data Summary
#
# {{data_summary_table_md}}

# %%
# <!-- PLOT:price_history -->
# Raw input data: close price + optional benchmark + conditional volume sub-row.
# Sanity-check view of the underlying BEFORE the equity curve. For options
# strategies this MUST be the underlying spot/futures, never the option chain.
import plotly.io as pio
fig = pio.from_json(open("results/plots/price_history.json").read())
fig.show()

# %% [markdown]
# <!-- SECTION:4:backtest_setup -->
# # 4. Backtest Setup
#
# {{execution_config_md}}
#
# {{signals_summary_md}}

# %% [markdown]
# <!-- SECTION:4b:stats_panel -->
# # 4b. Performance Stats (Strategy vs Buy & Hold)

# %%
# <!-- PLOT:stats_panel -->
import plotly.io as pio
fig = pio.from_json(open("results/plots/stats_panel.json").read())
fig.show()

# %% [markdown]
# <!-- SECTION:5:equity_curve -->
# # 5. Equity Curve and Benchmark

# %%
# <!-- PLOT:equity -->
import json, plotly.io as pio
fig = pio.from_json(open("results/plots/equity.json").read())
fig.show()

# %% [markdown]
# <!-- SECTION:6:drawdown -->
# # 6. Drawdown

# %%
# <!-- PLOT:drawdown -->
import plotly.io as pio
fig = pio.from_json(open("results/plots/drawdown.json").read())
fig.show()

# %% [markdown]
# <!-- SECTION:7:returns_tables -->
# # 7. Returns Tables

# %%
# <!-- PLOT:returns_heatmap -->
import plotly.io as pio
fig = pio.from_json(open("results/plots/returns_heatmap.json").read())
fig.show()

# %%
# <!-- PLOT:yearly_bars -->
import plotly.io as pio
fig = pio.from_json(open("results/plots/yearly_bars.json").read())
fig.show()

# %% [markdown]
# <!-- SECTION:8:metrics -->
# # 8. Metrics
#
# {{metrics_table_md}}

# %% [markdown]
# <!-- SECTION:9:trade_statistics -->
# # 9. Trade Statistics
#
# {{trade_stats_summary_md}}

# %%
# <!-- PLOT:trade_markers -->
import plotly.io as pio
fig = pio.from_json(open("results/plots/trade_markers.json").read())
fig.show()

# %%
# <!-- PLOT:hold_time_hist -->
import plotly.io as pio
fig = pio.from_json(open("results/plots/hold_time_hist.json").read())
fig.show()

# %% [markdown]
# <!-- SECTION:10:free_form -->
# # 10. Analysis Notes
#
# {{free_form_md}}

# %% [markdown]
# <!-- SECTION:11:iterations -->
# # 11. Iteration Log
#
# {{iterations_md}}
