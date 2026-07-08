import streamlit as st
import json
import random
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

st.set_page_config(page_title="临床试验随机化与项目管理系统", layout="wide")

# ==========================================
# 1. 终极断后方案：直接手动输入 NEON 连接串
# ==========================================
st.sidebar.header("🔑 数据库连接配置")

# 我们直接在侧边栏做两个选项，方便你一键切换和调试
conn_method = st.sidebar.radio("选择连接数据源方式：", ["直接硬编码（最稳妥）", "读取 Streamlit Secrets"])

if conn_method == "直接硬编码（最稳妥）":
    # 💡 请直接把下方双引号里的内容，替换为你从 Neon 复制出来的、真实的 Pooled 连接串！
    # ⚠️ 注意：开头一定要是 postgresql+pg8000://
    NEON_URL = "postgresql+pg8000://neondb_owner:npg_Mm9KXOpdu5qU@ep-cool-brook-atpw68yy-pooler.c-9.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require
    
    st.sidebar.info("👉 当前正在使用【硬编码】连接。请确保上方代码里的密码和域名已替换为您自己的 Neon 链接。")
    DB_URL = NEON_URL
else:
    # 从 secrets 读取
    if "postgres" in st.secrets:
        DB_URL = st.secrets["postgres"]["url"]
        st.sidebar.success("✅ 成功找到 Secrets 中的 postgres 字段")
    else:
        st.sidebar.error("❌ 未能在 Secrets 中找到配置！已拦截向 localhost 的连接。")
        st.error("无法读取 Secrets 配置，请在右侧选择【直接硬编码】并填入你的 Neon 连接串。")
        st.stop()

# ==========================================
# 2. 创建 Engine 与会话
# ==========================================
@st.cache_resource
def get_db_engine(url):
    # 使用 st.cache_resource 确保整个应用生命周期只创建一次连接池，避免 localhost 残留
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)

try:
    engine = get_db_engine(DB_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
except Exception as e:
    st.error(f"构建数据库引擎失败: {e}")
    st.stop()

def init_db():
    """初始化数据库表结构，显式对齐 Neon (PostgreSQL) 的 JSONB 类型"""
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                trial_id VARCHAR(100) UNIQUE NOT NULL,
                trial_name VARCHAR(255) NOT NULL,
                pi VARCHAR(100) NOT NULL,
                blind_level VARCHAR(50) NOT NULL,
                unblind_pwd VARCHAR(255) NOT NULL,
                strata_list TEXT NOT NULL,
                arms TEXT NOT NULL,
                ratios TEXT NOT NULL,
                seed_base INT NOT NULL,
                current_block_ids JSONB NOT NULL DEFAULT '{}'::jsonb,
                blinds_config JSONB NOT NULL DEFAULT '{}'::jsonb
            );
        """))
        conn.commit()

# 点击按钮或刷新时尝试初始化
if st.sidebar.button("🔄 强制重新初始化表结构"):
    try:
        init_db()
        st.sidebar.success("表结构初始化/检查成功！")
    except Exception as e:
        st.sidebar.error(f"表结构初始化失败: {e}")

# 无论如何，在主程序跑之前先静默建立一次，如果有错直接拦截
try:
    init_db()
except Exception as e:
    st.error("🚨 无法建立与 Neon 云数据库的通信。请确认您的 Neon 实例未休眠，且账户密码、URL 正确。")
    st.exception(e)
    st.stop()

# ==========================================
# 3. 核心业务逻辑：生成盲法盲号映射
# ==========================================
def generate_blinds_map(arms_list, blind_level, seed):
    random.seed(seed)
    blinds_map = {}
    
    if blind_level == "双盲 (Double Blind)":
        pool = []
        for arm in arms_list:
            pool.extend([(arm, random.randint(1000, 9999)) for _ in range(20)])
        random.shuffle(pool)
        for idx, (arm, code) in enumerate(pool):
            blinds_map[f"CODE_{code}"] = {"arm": arm, "sequence": idx + 1, "masked": True}
    elif blind_level == "单盲 (Single Blind)":
        for idx, arm in enumerate(arms_list * 10):
            blinds_map[f"SUBJ_{1000 + idx}"] = {"arm": arm, "sequence": idx + 1, "masked": False}
    else:
        blinds_map["config"] = {"status": "Open-Label", "arms": arms_list}
        
    return blinds_map

# ==========================================
# 4. 前端交互界面
# ==========================================
st.title("🧬 临床试验随机化中心管理系统")
st.caption("基于 Neon Serverless PostgreSQL 的多项目随机化终极版")

tab1, tab2 = st.tabs(["➕ 新建临床试验项目", "📂 查看与管理已有项目"])

with tab1:
    st.header("新建试验配置")
    with st.form("create_project_form"):
        col1, col2 = st.columns(2)
        with col1:
            new_id = st.text_input("试验方案编号 (Trial ID)*", placeholder="例如: CTR2026001")
            new_name = st.text_input("试验正式名称 (Trial Name)*", placeholder="例如: 某药物治疗XX病II期临床试验")
            new_pi = st.text_input("主要研究者 (PI)*", placeholder="张教授")
            new_blind_level = st.selectbox("盲态设计 (Blind Level)*", ["双盲 (Double Blind)", "单盲 (Single Blind)", "非盲 (Open Label)"])
            new_pwd = st.text_input("紧急解盲密码 (Unblinding Password)*", type="password")

        with col2:
            new_strata = st.text_area("分层因素配置 (Strata)", value="Center1, Center2")
            new_seed = st.number_input("随机化基础种子 (Seed Base)", value=12345, step=1)
            st.markdown("---")
            st.subheader("🎲 随机组与比例配置")
            
            num_arms = st.number_input("试验组别数量 (Number of Arms)", min_value=2, max_value=5, value=2, step=1)
            arms_inputs = []
            ratios_inputs = []
            
            arm_cols = st.columns(int(num_arms))
            for i in range(int(num_arms)):
                with arm_cols[i]:
                    default_name = "试验组(A)" if i == 0 else ("对照组(B)" if i == 1 else f"组别({chr(65+i)})")
                    arm_name = st.text_input(f"组别 {i+1} 名称", value=default_name, key=f"arm_{i}")
                    arm_ratio = st.number_input(f"权重", min_value=1, max_value=10, value=1, step=1, key=f"ratio_{i}")
                    arms_inputs.append(arm_name)
                    ratios_inputs.append(str(arm_ratio))

        submit_btn = st.form_submit_button("🛡️ 初始化项目并注入 Neon 数据库")

    if submit_btn:
        if not new_id or not new_name or not new_pi or not new_pwd:
            st.error("❌ 请填写所有带 * 的必填项！")
        else:
            valid_arms = [a.strip() for a in arms_inputs if a.strip()]
            valid_ratios = ratios_inputs[:len(valid_arms)]
            blinds_map = generate_blinds_map(valid_arms, new_blind_level, new_seed)
            
            session = SessionLocal()
            try:
                stmt = text("""
                    INSERT INTO projects (
                        trial_id, trial_name, pi, blind_level, unblind_pwd, 
                        strata_list, arms, ratios, seed_base, current_block_ids, blinds_config
                    )
                    VALUES (
                        :t, :name, :pi, :blind, :pwd, 
                        :strata, :arms, :ratios, :seed, :blocks::jsonb, :blinds::jsonb
                    );
                """)
                session.execute(stmt, {
                    "t": new_id, "name": new_name, "pi": new_pi, "blind": new_blind_level, "pwd": new_pwd,
                    "strata": new_strata, "arms": ",".join(valid_arms), "ratios": ",".join(valid_ratios), "seed": new_seed,
                    "blocks": json.dumps({}), "blinds": json.dumps(blinds_map)
                })
                session.commit()
                st.success(f"🎉 项目 [{new_id}] 初始化成功并成功导入 Neon 数据库！")
            except Exception as e:
                session.rollback()
                st.error("数据写入失败：")
                st.exception(e)
            finally:
                session.close()

with tab2:
    st.header("已有项目列表与状态")
    session = SessionLocal()
    try:
        projects = session.execute(text("SELECT trial_id, trial_name, pi, blind_level, strata_list, arms FROM projects;")).fetchall()
        if not projects:
            st.info("💡 暂无已初始化的项目。")
        else:
            for proj in projects:
                with st.expander(f"📁 项目: {proj[0]} — {proj[1]}"):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("主要研究者 (PI)", proj[2])
                    c2.metric("盲态级别", proj[3])
                    c3.metric("研究组别 (Arms)", proj[5])
                    st.markdown(f"**分层/中心列表:** `{proj[4]}`")
    except Exception as e:
        st.error(f"读取数据失败，请确认您的 Neon 数据库网络处于开启状态。")
        st.exception(e)
    finally:
        session.close()
