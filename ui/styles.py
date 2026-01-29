def get_custom_css():
    return """
    <style>
    /* Global Styles */
    .stApp {
        background-color: #0E1117;
        color: #FAFAFA;
        font-family: 'Inter', sans-serif;
    }
    
    /* Custom Card Style */
    .metric-card {
        background: linear-gradient(145deg, #1E1E1E, #252526);
        border-radius: 10px;
        padding: 20px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
        margin-bottom: 20px;
        border: 1px solid #333;
    }
    
    .metric-value {
        font-size: 2.2rem;
        font-weight: 700;
        background: -webkit-linear-gradient(45deg, #00C9FF, #92FE9D);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    
    .metric-label {
        font-size: 0.9rem;
        color: #A0A0A0;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    
    /* Charts */
    .js-plotly-plot .plotly .modebar {
        orientation: v;
        top: 0;
        right: -20px;
    }
    
    /* Sidebar */
    [data-testid="stSidebar"] {
        background-color: #16181D;
        border-right: 1px solid #333;
    }
    
    /* Accent Color for progress bars etc */
    .stProgress > div > div > div > div {
        background-color: #00C9FF;
    }
    
    h1, h2, h3 {
        color: #FAFAFA;
    }
    
    /* Utility */
    .positive { color: #00FF7F; }
    .negative { color: #FF4B4B; }
    
    /* Hide Streamlit Toolbar and Footer */
    [data-testid="stToolbar"] {
        visibility: hidden;
        height: 0px;
    }
    [data-testid="stHeader"] {
        visibility: hidden;
        height: 0px;
    }
    footer {
        visibility: hidden;
        height: 0px;
    }
    </style>
    """

def metric_card(label, value, delta=None):
    delta_html = ""
    if delta:
        color = "positive" if delta >= 0 else "negative"
        sign = "+" if delta >= 0 else ""
        delta_html = f"<div class='{color}' style='font-size: 0.85rem; margin-top: 5px;'>{sign}{delta}%</div>"
        
    return f"""
    <div class="metric-card">
        <div class="metric-label">{label}</div>
        <div class="metric-value">{value}</div>
        {delta_html}
    </div>
    """
