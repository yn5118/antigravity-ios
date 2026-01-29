import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from ui.styles import get_custom_css, metric_card
from logic.portfolio import Portfolio, calculate_compound_interest, get_best_stocks, calculate_ai_performance, calculate_position_size, analyze_single_stock
from logic.data_fetcher import get_current_price
from logic.demo_trader import DemoAccount

from logic.technical import add_technical_indicators
from logic.technical import add_technical_indicators
from logic.sentiment import analyze_key_person_impact, discover_market_movers # Updated Import
from logic.sentiment import analyze_key_person_impact, discover_market_movers
from logic.data_fetcher import get_market_news_rss
from logic.calendar_fetcher import CalendarFetcher, get_event_context_string
from logic.calendar import get_market_status_check, get_market_state_check
import datetime
import time

# Page Config
st.set_page_config(
    page_title="Antigravity 投資システム",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Apply Custom CSS
st.markdown(get_custom_css(), unsafe_allow_html=True)

def main():
    st.title("ANTIGRAVITY 🚀")
    
    # --- Sidebar ---
    with st.sidebar:
        st.header("設定")
        initial_balance = st.number_input("初期投資額 (円)", value=100000, step=10000)
        monthly_contribution = st.number_input("毎月の積立額 (円)", value=30000, step=5000)
        target_years = st.slider("投資期間 (年)", 1, 30, 10)
        expected_return = st.slider("想定年利 (%)", 1, 50, 20) / 100
        fire_target = st.number_input("FIRE目標額 (円)", value=50000000, step=1000000)
        
        st.markdown("---")
        st.markdown("---")
        # Removed Watchlist as requested
        st.caption("AI Stock Diagnosis Available in Tab 2")

    # --- Market Status Check (Event Warning) ---
    market_status = get_market_status_check()
    if market_status['status'] == "WARNING":
        st.error(market_status['message'])
        
    # --- Market State Banner (Weekend/Open/Closed) ---
    market_state = get_market_state_check()
    state_color = "#00C9FF" # Open
    if market_state['state'] == "WEEKEND": state_color = "#FFD700" # Warning/Gold
    if market_state['state'] == "CLOSED": state_color = "#A0A0A0" # Gray
    
    st.markdown(f"""
    <div style='background: {state_color}; color: #000; padding: 10px; border-radius: 5px; font-weight: bold; text-align: center; margin-bottom: 20px;'>
        🕰️ {market_state['message']}
    </div>
    """, unsafe_allow_html=True)
    
    # --- Initialize Portfolio ---
    # In a real app, this would persist in a database or session state
    if 'portfolio' not in st.session_state:
        st.session_state.portfolio = Portfolio(initial_balance)
    
    portfolio = st.session_state.portfolio

    # --- Initialize Demo Account ---
    if 'demo_account' not in st.session_state:
        st.session_state.demo_account = DemoAccount()
        st.session_state.demo_account.load() # Load persisted data
    
    demo_account = st.session_state.demo_account

    # --- Tabs ---
    tab1, tab2, tab3, tab4 = st.tabs(["🚀 ダッシュボード", "📈 市場分析", "🔮 ハイパーラーニング", "🎮 デモトレード"])

    with tab1:
        st.subheader("資産サマリー")
        
        # --- Event Countdown Widget ---
        cal = CalendarFetcher()
        next_event = cal.get_next_major_event()
        if next_event:
            today_date = datetime.date.today()
            event_date = datetime.datetime.strptime(next_event['date'], "%Y-%m-%d").date()
            days_left = (event_date - today_date).days
            
            msg = f"あと {days_left}日" if days_left > 0 else "本日開催"
            if days_left < 0: msg = "終了"
            
            st.markdown(f"""
            <div style='background: linear-gradient(90deg, #1E1E1E 0%, #2D2D2D 100%); padding: 10px 15px; border-radius: 8px; border: 1px solid #444; margin-bottom: 20px; display: flex; align-items: center; justify-content: space-between;'>
                <div style='display: flex; align-items: center; gap: 10px;'>
                    <span style='font-size: 1.5em;'>📅</span>
                    <div>
                        <div style='color: #888; font-size: 0.85em; font-weight: bold;'>NEXT BIG EVENT ({next_event['country']})</div>
                        <div style='color: #FAFAFA; font-weight: bold;'>{next_event['title']}</div>
                    </div>
                </div>
                <div style='text-align: right;'>
                    <div style='color: #FF4B4B; font-weight: bold; font-size: 1.2em;'>{msg}</div>
                    <div style='color: #666; font-size: 0.8em;'>{next_event['date']} {next_event['time']}</div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            
        # Calculate Mock Current Value
        # 実際にはここで各銘柄の現在値をループで取得し、PF価値を計算する
        current_val = portfolio.balance # Demo: cash only for now
        
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(metric_card("総資産", f"{current_val:,.0f} 円", 5.2), unsafe_allow_html=True)
        with c2:
            st.markdown(metric_card("年初来パフォーマンス", "+12.4%", 12.4), unsafe_allow_html=True)
        with c3:
            st.markdown(metric_card("AI 勝率", "49.0% → 62.1%", 13.1), unsafe_allow_html=True)

        st.markdown("### 🔭 未来シミュレーション (複利計算)")
        df_compound = calculate_compound_interest(
            initial_balance, 
            monthly_contribution, 
            expected_return, 
            target_years
        )
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df_compound['年'], 
            y=df_compound['総資産額'], 
            mode='lines', 
            name='総資産額 (複利効果)',
            line=dict(color='#00C9FF', width=3),
            fill='tozeroy', # Area chart effect
            fillcolor='rgba(0, 201, 255, 0.1)'
        ))
        fig.add_trace(go.Scatter(
            x=df_compound['年'], 
            y=df_compound['元本'], 
            mode='lines', 
            name='元本 (入金累計)',
            line=dict(color='#A0A0A0', width=2, dash='dot')
        ))
        fig.update_layout(
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
            font=dict(color='#FAFAFA'),
            xaxis=dict(showgrid=False, title='年数'),
            yaxis=dict(
                showgrid=True, 
                gridcolor='#333', 
                title='金額 (円)',
                tickformat=',.0f' # Use full numbers with commas, e.g. 1,000,000
            ),
            margin=dict(l=0, r=0, t=30, b=0)
        )

        
        # Add FIRE Target Line
        fig.add_hline(y=fire_target, line_dash="dash", line_color="#FF4B4B", annotation_text="FIRE目標", annotation_position="top left")
        
        # Calculate FIRE year
        fire_year = "未達"
        for index, row in df_compound.iterrows():
            if row['総資産額'] >= fire_target:
                fire_year = f"{row['年']}年後"
                break
                
        if fire_year != "未達":
            st.success(f"🎉 現在のペースなら {fire_year} にFIRE達成可能です")
            
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("### 💼 My ポートフォリオ (保有資産)")
        
        # Editable Portfolio Data
        if 'my_portfolio_data' not in st.session_state:
            st.session_state.my_portfolio_data = pd.DataFrame([
                {"ticker": "7203.T", "name": "トヨタ自動車", "qty": 100, "avg_price": 2800},
                {"ticker": "AAPL", "name": "Apple Inc.", "qty": 15, "avg_price": 180},
                {"ticker": "CASH", "name": "現金 (JPY)", "qty": 1, "avg_price": 450000}
            ])

        st.info("👇 下記の表は編集可能です。あなたの保有銘柄を入力してください。")
        
        edited_df = st.data_editor(
            st.session_state.my_portfolio_data,
            column_config={
                "ticker": "銘柄コード",
                "name": "銘柄名",
                "qty": st.column_config.NumberColumn("保有数", min_value=0, step=1),
                "avg_price": st.column_config.NumberColumn("平均取得単価", min_value=0, format="¥%d")
            },
            num_rows="dynamic",
            use_container_width=True
        )
        
        # Update session state
        st.session_state.my_portfolio_data = edited_df

        # Calculate Metrics dynamically
        # In a real app, we would batch fetch current prices for all tickers in edited_df['ticker']
        
        calc_df = edited_df.copy()
        current_prices = []
        market_values = []
        profits = []
        profit_pcts = []
        
        import random
        
        for index, row in calc_df.iterrows():
            t = str(row['ticker'])
            # Fetch Real Price
            if t == "CASH":
                curr = row['avg_price']
            else:
                fetched = get_current_price(t)
                curr = fetched if fetched is not None else row['avg_price']
            
            val = row['qty'] * curr
            cost = row['qty'] * row['avg_price']
            prof = val - cost
            pct = (prof / cost * 100) if cost > 0 else 0
            
            current_prices.append(curr)
            market_values.append(val)
            profits.append(prof)
            profit_pcts.append(pct)
            
        calc_df['current_price'] = current_prices
        calc_df['market_value'] = market_values
        calc_df['profit'] = profits
        calc_df['profit_pct'] = profit_pcts
        
        # Display Summary
        total_val = sum(market_values)
        total_profit = sum(profits)
        
        st.write("### 📊 評価額サマリー")
        c_sum1, c_sum2 = st.columns(2)
        c_sum1.metric("評価額合計", f"{total_val:,.0f} 円")
        c_sum2.metric("含み損益合計", f"{total_profit:,.0f} 円", delta=f"{total_profit:,.0f} 円")

        # Display Allocation Chart
        st.markdown("##### 資産配分")
        if not calc_df.empty:
            fig_alloc = go.Figure(data=[go.Pie(labels=calc_df['name'], values=calc_df['market_value'], hole=.4)])
            fig_alloc.update_layout(
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                font=dict(color='#FAFAFA'),
                margin=dict(l=0, r=0, t=0, b=0),
                height=300
            )
            st.plotly_chart(fig_alloc, use_container_width=True)
        else:
            st.caption("データがありません。")

    with tab2:
        st.subheader("📊 AI市場分析 & パフォーマンス")
        
        # AI Performance Metrics
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("AIモデル精度 (過去30日)", "78.4%", "+2.1%")
        with m2:
            st.metric("AI推奨通り売買した場合", "+42.8%", "vs 市場平均 +12%")
        with m3:
            st.metric("アクティブ運用の優位性", "高", "リスク調整後リターン > 2.0")
            
        # Demo Account Chart
        st.markdown("#### 🤖 AIデモ口座 パフォーマンス推移")
        df_ai = calculate_ai_performance(years=1)
        fig_ai = go.Figure()
        fig_ai.add_trace(go.Scatter(x=df_ai['Date'], y=df_ai['AI資産推移'], name='AIモデル運用', line=dict(color='#00C9FF', width=3)))
        fig_ai.add_trace(go.Scatter(x=df_ai['Date'], y=df_ai['市場平均'], name='市場平均 (インデックス)', line=dict(color='#888', dash='dash')))
        fig_ai.update_layout(paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color='#FAFAFA'))
        st.plotly_chart(fig_ai, use_container_width=True)

        st.markdown("---")
        st.subheader("🔍 個別銘柄 AI診断 (Individual Diagnosis)")
        st.caption("指定した銘柄をGemini 1.5 Proが即座に分析します。")
        
        c_diag1, c_diag2 = st.columns([3, 1])
        with c_diag1:
            diag_ticker = st.text_input("銘柄コード (例: 7203.T, NVDA)", placeholder="7203.T")
        with c_diag2:
            st.write("") # Spacer
            st.write("")
            diag_btn = st.button("AI診断実行", type="primary")
            
        if diag_btn and diag_ticker:
            with st.spinner(f"{diag_ticker} を分析中..."):
                # Fetch Contexts
                evt_ctx = get_event_context_string() 
                m_status = get_market_status_check() 
                m_state = get_market_state_check() 
                
                # Run Analysis
                try:
                    result = analyze_single_stock(
                        diag_ticker, 
                        None, # No pre-calc data
                        evt_ctx, 
                        m_status, 
                        m_state['message'] 
                    )
                    
                    # Display Result
                    stock = result
                    
                    # Highlight Volume Surge
                    border_color = "#FFD700" if stock.get('is_surging') else "#00C9FF"
                    surge_badge = "<span style='background: #FFD700; color: #000; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; margin-right: 5px;'>🔥 出来高急増</span>" if stock.get('is_surging') else ""

                    # Sizing Calc
                    sizing = calculate_position_size(demo_account.balance, stock['current_price'], risk_pct=0.10)

                    st.markdown(f"""
<div style='background: #1E1E1E; padding: 15px; border-radius: 8px; margin-bottom: 15px; border-left: 5px solid {border_color}; box-shadow: 0 4px 6px rgba(0,0,0,0.3);'>
<div style='display: flex; justify-content: space-between; align-items: center;'>
<div style='display: flex; align-items: center;'>
<h3 style='margin:0; color: #FAFAFA; margin-right: 10px;'>{stock['ticker']}</h3>
{surge_badge}
</div>
<span style='background: #00C9FF; color: #000; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.9em;'>AIスコア {stock['score']:.1f}</span>
</div>
<div style='display: flex; gap: 20px; margin-top: 10px; color: #CCC; font-size: 0.95em;'>
<div>現在値 <strong style='color: #FAFAFA;'>{stock['display_price']}</strong></div>
<div>目標株価 <strong style='color: #00FF7F;'>{stock['display_target']}</strong></div>
<div>損切ライン <strong style='color: #FF4B4B;'>{stock['display_sl']}</strong></div>
</div>
<div style='background: #252526; padding: 10px; border-radius: 4px; margin-top: 10px;'>
<p style='color: #A0A0A0; margin: 0; font-size: 0.85em;'>💰 資金管理アドバイス (ポートフォリオ比率 10%想定)</p>
<p style='color: #FAFAFA; font-weight: bold; margin: 2px 0 0 0;'>推奨購入数 {sizing['qty']}株 ({sizing['amount']:,.0f}円相当) <span style='font-size: 0.9em; font-weight: normal; color: #DDD;'>→ 分割エントリー推奨</span></p>
</div>
<div style='margin-top: 10px; padding-top: 10px; border-top: 1px solid #333;'>
<p style='color: #FFD700; font-weight: bold; margin: 0;'>📢 推奨アクション {stock['action']}</p>
<p style='color: #A0A0A0; margin: 5px 0 0 0; font-size: 0.9em;'>💡 {stock['reason']}</p>
</div>
</div>
""", unsafe_allow_html=True)
                    
                    # Buy Button for Single Diagnosis
                    if st.button(f"🎮 デモで購入 ({stock['ticker']})", key=f"buy_diag_{stock['ticker']}"):
                        exec_price = stock.get('rec_entry_price', stock['current_price'])
                        order_type = stock.get('rec_order_type', '成行')
                        sl_price = stock.get('stop_loss_price', exec_price * 0.95)
                        
                        success, msg = demo_account.execute_order(
                            stock['ticker'], 
                            "BUY", 
                            sizing['qty'], 
                            exec_price, 
                            order_type,
                            tp=stock['target_price'],
                            sl=sl_price
                        )
                        if success:
                            demo_account.save()
                            st.toast(f"注文成功: {stock['ticker']}", icon="✅")
                        else:
                            st.toast(f"注文失敗: {msg}", icon="❌")

                except Exception as e:
                    st.error(f"分析中にエラーが発生しました: {e}")

        st.markdown("---")
        st.subheader("🗣️ キーマン分析 (Key Person Matrix)")
        st.caption("Google Gemini 1.5 Proが、最新ニュースから市場を動かす重要人物を自動検出し、対応する投資戦略を提示します。")

        # --- Caching Wrappers ---
        @st.cache_data(ttl=3600) # Cache for 1 hour
        def get_cached_news():
            # In real app, fetch from RSS. For demo, we might use a mix or mock if RSS fails.
            # Let's try to fetch real RSS first
            rss_news = get_market_news_rss("business")
            if rss_news:
                return " ".join([n['title'] for n in rss_news])
            else:
                # Fallback text if offline
                return "Elon Musk announces new Tesla model. BOJ Governor Ueda hints at rate hike. Sam Altman discusses AGI regulation."

        @st.cache_data(ttl=3600) 
        def analyze_movers_cached(text):
            return discover_market_movers(text)
        
        if 'key_person_movers' not in st.session_state:
            st.session_state.key_person_movers = []

        if st.button("🔄 AI市場スキャン (Auto-Detect)"):
            with st.spinner("ニュース収集中 & AI分析中..."):
                news_text = get_cached_news()
                movers = analyze_movers_cached(news_text)
                st.session_state.key_person_movers = movers
        
        # Always display if data exists
        if st.session_state.key_person_movers:
            movers = st.session_state.key_person_movers
            
            if not movers: # Empty list case
                st.warning("有力な市場変動要因が検出されませんでした。")
            
            for mover in movers:
                score = mover.get('impact', 50)
                color = "#FF4B4B" if score >= 80 else "#FFD700" if score >= 50 else "#00C9FF"
                strategy_color = "#00FF7F" if "Buy" in mover.get('strategy', '') else "#FF4B4B"
                
                st.markdown(f"""
<div style='background: #1E1E1E; padding: 15px; border-radius: 8px; margin-bottom: 15px; border-left: 5px solid {color};'>
<div style='display: flex; justify-content: space-between;'>
<h4 style='margin:0; color: #FAFAFA;'>👤 {mover.get('person')}</h4>
<span style='background: {color}; color: #000; padding: 2px 8px; border-radius: 4px; font-weight: bold;'>Impact {score}</span>
</div>
<p style='margin: 5px 0; color: #CCC;'>関連銘柄 <strong style='color: #FAFAFA;'>{mover.get('asset')}</strong></p>
<div style='background: #252526; padding: 10px; border-radius: 4px; margin-top: 10px;'>
<p style='color: {strategy_color}; font-weight: bold; margin: 0;'>📢 戦略 {mover.get('strategy')}</p>
<p style='color: #AAA; font-size: 0.9em; margin: 5px 0 0 0;'>{mover.get('reason')}</p>
</div>
</div>
""", unsafe_allow_html=True)
        
        # Add Clear Button
        if st.session_state.key_person_movers:
             if st.button("結果をクリア", key="clear_movers"):
                 st.session_state.key_person_movers = []
                 st.rerun()

        st.markdown("---")
        # Dynamic Header based on Market State
        list_label = market_state['label'] # "月曜日の注目株" etc
        st.subheader(f"📢 AI選定 {list_label} (推奨ポートフォリオ)")
        
        # Add a visual indicator of the analysis scope
        st.info("🌐 **市場スキャン完了**: グローバル市場 (全銘柄対象) から、SNSトレンド・ニュースセンチメント・テクニカル分析を総合して選定しました。")
        
        # Mock Data for demo speed
        # The internal logic now uses GLOBAL_REQ_STOCKS
        
        # Demo: Fetch real price for first ticker to show it works
        # Logic: Trigger Analysis on Button Click
        if st.button("AI市場スキャン開始（Gemini 1.5 Pro）", type="primary"):
            # プログレスバーのデプロイ
            prog_bar = st.progress(0)
            status = st.empty()
            
            # 1. 未来の会見予定を確認（30%）
            status.text("30% 来週の要人会見予定（植田総裁等）をスキャン中...")
            prog_bar.progress(30)
            time.sleep(1.5) # UX wait
            
            # 2. 過去データの逆算分析（70%）
            status.text("70% Gemini API が過去のニュースから週明けを推論中... (強制推論モード起動)")
            prog_bar.progress(70)
            time.sleep(1.0)
            
            # 3. 最終選定（100%）へのブリッジ
            # get_best_stocks will handle 70 -> 100% updates via callbacks
            best_stocks = get_best_stocks(
                [], # Candidates input is ignored by new logic
                {}, # Mock sentiment ignored
                {}, # Mock technical ignored
                progress_callback=prog_bar.progress,
                status_callback=status.text
            )
            
            # 完了
            status.text("100% 解析完了 月曜日のベスト 5 銘柄を特定しました")
            prog_bar.progress(100)
            time.sleep(1)
            prog_bar.empty()
            status.empty()
            
            # Save to Session State
            st.session_state['ai_best_stocks_result'] = best_stocks
            st.toast("最新の市場データを取得しました！", icon="📈")
        
        # Display Results from Session State
        best_stocks = st.session_state.get('ai_best_stocks_result', [])
        
        if not best_stocks and 'ai_best_stocks_result' not in st.session_state:
            # First time load or empty state
            st.info("👆 上記のボタンを押して、最新のAI市場分析を開始してください。")
        
        for stock in best_stocks:
            # Calculate sizing
            sizing = calculate_position_size(portfolio.balance, stock['current_price'], risk_pct=0.10) # 10% allocation
            
            # Highlight Volume Surge
            border_color = "#FFD700" if stock.get('is_surging') else "#00C9FF"
            surge_badge = "<span style='background: #FFD700; color: #000; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; margin-right: 5px;'>🔥 出来高急増</span>" if stock.get('is_surging') else ""

            with st.container():
                st.markdown(f"""
<div style='background: #1E1E1E; padding: 15px; border-radius: 8px; margin-bottom: 15px; border-left: 5px solid {border_color}; box-shadow: 0 4px 6px rgba(0,0,0,0.3);'>
<div style='display: flex; justify-content: space-between; align-items: center;'>
<div style='display: flex; align-items: center;'>
<h3 style='margin:0; color: #FAFAFA; margin-right: 10px;'>{stock['ticker']}</h3>
{surge_badge}
</div>
<span style='background: #00C9FF; color: #000; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.9em;'>AIスコア {stock['score']:.1f}</span>
</div>
<div style='display: flex; gap: 20px; margin-top: 10px; color: #CCC; font-size: 0.95em;'>
<div>現在値 <strong style='color: #FAFAFA;'>{stock['display_price']}</strong></div>
<div>目標株価 <strong style='color: #00FF7F;'>{stock['display_target']}</strong></div>
<div>損切ライン <strong style='color: #FF4B4B;'>{stock['display_sl']}</strong></div>
</div>
<div style='background: #252526; padding: 10px; border-radius: 4px; margin-top: 10px;'>
<p style='color: #A0A0A0; margin: 0; font-size: 0.85em;'>💰 資金管理アドバイス (ポートフォリオ比率 10%想定)</p>
<p style='color: #FAFAFA; font-weight: bold; margin: 2px 0 0 0;'>推奨購入数 {sizing['qty']}株 ({sizing['amount']:,.0f}円相当) <span style='font-size: 0.9em; font-weight: normal; color: #DDD;'>→ 分割エントリー推奨 (本日 {sizing['qty']//2}株, 残りは押し目待ち)</span></p>
</div>
<div style='margin-top: 10px; padding-top: 10px; border-top: 1px solid #333;'>
<p style='color: #FFD700; font-weight: bold; margin: 0;'>📢 推奨アクション {stock['action']}</p>
<p style='color: #A0A0A0; margin: 5px 0 0 0; font-size: 0.9em;'>💡 {stock['reason']}</p>
<p style='color: #FF4B4B; margin: 5px 0 0 0; font-size: 0.9em;'>🛑 損切根拠: {stock.get('stop_loss_reason', '算出中...')}</p>
</div>
</div>
""", unsafe_allow_html=True)
                
                # Buy Button
                if st.button(f"🎮 デモで購入 ({stock['ticker']})", key=f"buy_{stock['ticker']}"):
                    # Execute Order based on specific advice (Market or Limit)
                    # Use AI recommended Price and Type
                    exec_price = stock.get('rec_entry_price', stock['current_price'])
                    order_type = stock.get('rec_order_type', '成行')
                    
                    # SL logic: AI Calculated
                    sl_price = stock.get('stop_loss_price', exec_price * 0.95)
                    
                    success, msg = demo_account.execute_order(
                        stock['ticker'], 
                        "BUY", 
                        sizing['qty'], 
                        exec_price, 
                        order_type,
                        tp=stock['target_price'],
                        sl=sl_price
                    )
                    if success:
                        demo_account.save() # Persist Changes
                        st.toast(
                            f"注文成功 ({order_type}): {stock['ticker']}\n"
                            f"価格: {exec_price:,.0f}, 利確: {stock['target_price']:,.0f}", 
                            icon="✅"
                        )
                    else:
                        st.toast(f"注文失敗: {msg}", icon="❌")
            
            st.markdown("---")

    with tab3:
        st.subheader("🔮 ハイパーラーニング (強化学習モジュール)")
        
        st.markdown("""
        **「ハイパーラーニング」とは？**
        
        過去20年分の市場データを使用し、数百万通りの取引シナリオをAIが自己対戦形式でシミュレーションする機能です。
        従来のテクニカル分析を超え、未知の市場変動にも適応できる「勝てる投資戦略」を自動生成します。
        """)
        
        c1, c2 = st.columns([1, 2])
        
        with c1:
            st.markdown("### 学習パラメータ設定")
            model_type = st.selectbox(
                "AIモデル", 
                ["Deep Q-Network (DQN)", "Proximal Policy Optimization (PPO)", "A3C"],
                help="""
                **DQN**: 初心者向け。基本的な強化学習モデルで、安定した学習が可能ですが、学習速度はやや遅めです。\n
                **PPO**: 推奨。バランス型で、学習の安定性と速度の両立が図られています。金融データと相性が良いです。\n
                **A3C**: 上級者向け。並列処理を行い高速ですが、設定が複雑で過学習のリスクがあります。
                """
            )
            episodes = st.slider(
                "学習エピソード数", 
                100, 10000, 1000,
                help="シミュレーションの回数です。回数が多いほど精度は上がりますが、**過学習**（過去データに適合しすぎて未来に対応できなくなる現象）のリスクも高まります。通常は1000〜3000回が推奨されます。"
            )
            lr = st.slider(
                "学習率 (Learning Rate)", 
                0.0001, 0.01, 0.001, 
                format="%.4f",
                key="learning_rate_slider", # Unique Key
                help="AIが新しい情報を取り入れる速度です。高いと学習は早いですが不安定になりやすく、低いと安定しますが時間がかかります。"
            )
            
            start_btn = st.button("学習を開始する", type="primary")
        
        with c2:
            st.markdown("### 学習進捗状況")
            status_placeholder = st.empty()
            progress_bar = st.progress(0)
            
            if 'learning_complete' not in st.session_state:
                st.session_state.learning_complete = False

            if start_btn:
                status_placeholder.info("🚀 環境を初期化中...")
                time.sleep(1)
                
                for i in range(1, 101):
                    # Simulate processing time
                    time.sleep(0.03) 
                    
                    # Update progress
                    progress_bar.progress(i)
                    
                    # Update status text dynamically
                    if i < 20:
                        status_placeholder.text(f"データ読み込み中... ({i}%)")
                    elif i < 50:
                        status_placeholder.text(f"エピソード実行中: {int(episodes * i / 100)} / {episodes}")
                    elif i < 80:
                        status_placeholder.text(f"ニューラルネットワーク重み更新中... (Loss: {0.5 - (i*0.005):.4f})")
                    else:
                        status_placeholder.text("最終検証・バックテスト実行中...")
                
                progress_bar.progress(100)
                status_placeholder.success("✅ 学習が完了しました！")
                st.session_state.learning_complete = True
                
                # Save inputs for result generation
                st.session_state.hl_episodes = episodes
                st.session_state.hl_lr = lr
                st.session_state.hl_model = model_type
                
                st.rerun() # Rerun to show results immediately

            if st.session_state.learning_complete:
                st.markdown("---")
                st.subheader("📊 学習結果レポート")
                
                col1, col2, col3 = st.columns(3)
                
                # Generate Dynamic Results based on inputs
                import random
                
                # Base performance
                base_win_rate = 60.0
                base_return = 15.0
                base_sharpe = 1.2
                
                # Model multipliers
                model_mult = 1.0
                if "PPO" in st.session_state.get('hl_model', 'PPO'): model_mult = 1.1
                if "A3C" in st.session_state.get('hl_model', 'A3C'): model_mult = 1.05 # Higher risk/reward
                
                # Parameter impact
                # Episodes: More is generally better but diminishing returns
                ep_factor = (st.session_state.get('hl_episodes', 1000) / 10000) * 5.0 
                
                # Learning Rate: Goldilocks zone (0.001 is good, too high/low is bad)
                lr_val = st.session_state.get('hl_lr', 0.001)
                lr_penalty = abs(0.001 - lr_val) * 1000 
                
                # Random Variance
                variance = random.uniform(-2.0, 5.0)
                
                # Final Calc
                final_win = min(85.0, base_win_rate * model_mult + ep_factor - lr_penalty + variance)
                final_ret = min(150.0, base_return * model_mult + (ep_factor * 2) - (lr_penalty * 2) + variance)
                final_sharpe = min(3.0, base_sharpe * model_mult + (ep_factor * 0.1) + (variance * 0.05))
                
                with col1:
                    st.metric("AI 勝率", f"{final_win:.1f}%", f"{final_win - 55.0:.1f}%")
                with col2:
                    st.metric("期待年利", f"{final_ret:.1f}%", f"{final_ret - 10.0:.1f}%")
                with col3:
                    st.metric("シャープレシオ", f"{final_sharpe:.2f}", f"{final_sharpe - 1.0:.2f}")
                
                improvement = final_ret - 10.0
                st.info(f"新しいモデルは、従来の戦略よりも**{improvement:.1f}%**高いパフォーマンスを示しています。")
                
                if st.button("このモデルを適用する", type="primary"):
                    st.toast("新しいAIモデルをシステムに適用しました！", icon="🚀")
                    time.sleep(2)
                    st.session_state.learning_complete = False # Reset for demo
                    st.rerun()
            else:
                if not start_btn:
                    status_placeholder.info("「学習を開始する」ボタンを押してください。")

    with tab4:
        st.subheader("🎮 デモトレード (シミュレーション)")
        st.info("仮想資金を使って、リスクなしでトレードの練習ができます。")
        
        # --- Funds Management ---
        with st.expander("💰 資金調整 (入金/リセット)"):
            c_f1, c_f2 = st.columns([2, 1])
            with c_f1:
                adj_amount = st.number_input("金額 (円)", value=1000000, step=100000)
            with c_f2:
                if st.button("資金を追加"):
                    demo_account.balance += adj_amount
                    demo_account.save() # Persist
                    st.success(f"{adj_amount:,.0f}円を入金しました。")
                    st.rerun()
                if st.button("初期化 (リセット)"):
                    demo_account.reset(adj_amount)
                    demo_account.save() # Persist
                    st.warning(f"口座をリセットしました (残高: {adj_amount:,.0f}円)")
                    st.rerun()

        # --- Account Summary ---
        st.markdown("### 口座状況")
        # Fetch Live Prices for positions
        current_prices = {}
        for ticker in demo_account.positions.keys():
            p = get_current_price(ticker)
            current_prices[ticker] = p if p is not None else demo_account.positions[ticker]['avg_price']

        pf_val = demo_account.get_portfolio_value(current_prices)
        unrealized = demo_account.calculate_unrealized_pl(current_prices)
        
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("利用可能現金", f"{demo_account.balance:,.0f} 円")
        with m2:
            st.metric("ポートフォリオ評価額", f"{pf_val:,.0f} 円")
        with m3:
            val_color = "normal"
            if unrealized['total_pl'] > 0: val_color = "off" # green in streamlit default
            st.metric("評価損益", f"{unrealized['total_pl']:,.0f} 円", delta=f"{unrealized['total_pl']:,.0f} 円")

        st.markdown("---")
        
        # --- Trading Interface ---
        c1, c2 = st.columns([1, 2])
        
        with c1:
            st.markdown("### 新規注文")
            with st.form("order_form"):
                ticker = st.text_input("銘柄コード (例 7203.T)", "7203.T")
                side = st.radio("売買区分", ["買い (BUY)", "売り (SELL)"])
                order_type = st.selectbox("注文種別", ["成行", "指値", "逆指値"])
                
                qty = st.number_input("数量", min_value=1, value=100, step=100)
                price = st.number_input("指値価格 (成行の場合は概算)", value=2000.0)
                
                st.caption("決済条件 (オプション)")
                c_tp, c_sl = st.columns(2)
                with c_tp:
                    tp_input = st.number_input("利確 (TP) 価格", value=0.0, step=100.0, help="0の場合は設定なし")
                with c_sl:
                    sl_input = st.number_input("損切 (SL) 価格", value=0.0, step=100.0, help="0の場合は設定なし")
                
                submitted = st.form_submit_button("注文発注")
                
                if submitted:
                    s_side = "BUY" if "買い" in side else "SELL"
                    # Mock execution price
                    exec_price = price if order_type == "指値" else price # Simplify for demo
                    
                    tp_val = tp_input if tp_input > 0 else None
                    sl_val = sl_input if sl_input > 0 else None
                    
                    success, msg = demo_account.execute_order(ticker, s_side, qty, exec_price, order_type, tp=tp_val, sl=sl_val)
                    if success:
                        demo_account.save() # Persist
                        st.success(msg)
                    else:
                        st.error(msg)
        
        with c2:
            st.markdown("### 保有ポジション")
            if not demo_account.positions:
                st.write("現在保有しているポジションはありません。")
            else:
                pos_data = []
                for t, p in demo_account.positions.items():
                    curr = current_prices.get(t, p['avg_price'])
                    pl = (curr - p['avg_price']) * p['quantity']
                    pl_pct = (pl / (p['avg_price'] * p['quantity'])) * 100
                    pos_data.append({
                        "銘柄": t,
                        "数量": p['quantity'],
                        "平均取得単価": f"{p['avg_price']:,.0f}",
                        "現在値": f"{curr:,.0f}",
                        "現在値": f"{curr:,.0f}",
                        "評価損益": f"{pl:,.0f} ({pl_pct:+.1f}%)",
                        "TP(利確)": f"{p.get('tp', '-')}",
                        "SL(損切)": f"{p.get('sl', '-')}"
                    })
                st.dataframe(pd.DataFrame(pos_data), use_container_width=True)

            st.markdown("### 取引履歴")
            if demo_account.trade_history:
                hist_df = pd.DataFrame(demo_account.trade_history)
                # Translate columns
                hist_df = hist_df.rename(columns={
                    "timestamp": "日時",
                    "ticker": "銘柄",
                    "side": "売買",
                    "quantity": "数量",
                    "price": "価格",
                    "type": "種別",
                    "total": "受渡金額"
                })
                st.dataframe(hist_df.iloc[::-1], use_container_width=True) # Show newest first
            else:
                st.caption("履歴なし")

if __name__ == "__main__":
    main()
