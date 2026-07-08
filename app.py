import streamlit as st
import json
import random
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ==========================================
# 1. 数据库配置与初始化
# ==========================================
# 优先读取 Streamlit Secrets 中的数据库连接串，本地测试可替换为 sqlite 或本地 pg
if "postgres" in st.secrets:
    DB_URL = st.secrets["postgres"]["url"]
else:
    # 本地开发测试默认 fallback
    DB_URL = "postgresql+pg8000://postgres:password@localhost:5432/clinical_trials"

engine = create_engine(DB_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    """初始化数据库表结构，确保兼容 PostgreSQL 的 JSONB 类型"""
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

# 执行数据库初始化
try:
    init_db()
except Exception as e:
    st.error(f"数据库初始化失败，请检查连接配置: {e}")

# ==========================================
# 2. 核心核心业务逻辑：生成盲法盲号映射
# ==========================================
def generate_blinds_map(arms_list, blind_level, seed):
    """
    根据盲态级别和设定的随机组生成盲号映射表
    """
    random.seed(seed)
    blinds_map = {}
    
    if blind_level == "双盲 (Double Blind)":
        # 双盲情况下，为每个组分配独立的、被掩盖的试验编码（例如 A组对应 01, 04... B组对应 02, 03...）
        # 这里模拟生成 100 个交叉盲号作为示例
        pool = []
        for arm in arms_list:
            pool.extend([(arm, random.randint(1000, 9999)) for _ in range(20)])
        random.shuffle(pool)
        
        for idx, (arm, code) in enumerate(pool):
            blinds_map[f"CODE_{code}"] = {
                "arm": arm,
                "sequence": idx + 1,
                "masked": True
            }
    elif blind_level == "单盲 (Single Blind)":
        # 单盲通常患者不知情，研究者知情，映射直接保留组别，但加设随机化编号
        for idx, arm in enumerate(arms_list * 10):
            blinds_map[f"SUBJ_{1000 + idx}"] = {
                "arm": arm,
                "sequence": idx + 1,
                "masked": False
            }
    else:
        # 非盲 (Open Label)
        blinds_map["config"] = {"status": "Open-Label", "arms": arms_list}
        
    return blinds_map

# ==========================================
# 3. Streamlit 前端交互界面
# ==========================================
st.set_page_config(page_title="临床试验随机化与项目管理系统", layout="wide")
st.title("🧬 临床试验随机化中心管理系统")
st.caption("支持多项目独立管理、分层区组随机化、动态盲态配置及安全解盲")

# 使用 Tabs 区分“创建新项目”与“已有项目查看”
tab1, tab2 = st.tabs(["➕ 新建临床试验项目", "📂 查看与管理已有项目"])

with tab1:
    st.header("新建试验配置")
    
    with st.form("create_project_form"):
        col1, col2 = st.columns(2)
        with col1:
            new_id = st.text_input("试验方案编号 (Trial ID)*", placeholder="例如: CTR2026001")
            new_name = st.text_input("试验正式名称 (Trial Name)*", placeholder="例如: 某药物治疗XX病II期临床试验")
            new_pi = st.text_input("主要研究者 (PI)*", placeholder="张教授")
            
            # 盲法选择交互
            new_blind_level = st.selectbox(
                "盲态设计 (Blind Level)*",
                ["双盲 (Double Blind)", "单盲 (Single Blind)", "非盲 (Open Label)"]
            )
            new_pwd = st.text_input("紧急解盲密码 (Unblinding Password)*", type="password", help="用于该项目紧急破盲时验证")

        with col2:
            new_strata = st.text_area("分层因素配置 (Strata)", value="Center1, Center2", help="多个中心或层用逗号分隔")
            new_seed = st.number_input("随机化基础种子 (Seed Base)", value=12345, step=1)
            
            st.markdown("---")
            st.subheader("🎲 随机组与比例配置")
            
            # 动态选择随机组的数量
            num_arms = st.number_input("试验组别数量 (Number of Arms)", min_value=2, max_value=5, value=2, step=1)
            
            # 根据数量动态渲染输入框
            arms_inputs = []
            ratios_inputs = []
            
            arm_cols = st.columns(int(num_arms))
            for i in range(int(num_arms)):
                with arm_cols[i]:
                    default_name = "试验组(A)" if i == 0 else ("对照组(B)" if i == 1 else f"组别({chr(65+i)})")
                    arm_name = st.text_input(f"组别 {i+1} 名称", value=default_name, key=f"arm_{i}")
                    arm_ratio = st.number_input(f"权重比例", min_value=1, max_value=10, value=1, step=1, key=f"ratio_{i}")
                    arms_inputs.append(arm_name)
                    ratios_inputs.append(str(arm_ratio))

        submit_btn = st.form_submit_with_repr = st.form_submit_button("🛡️ 初始化项目并注入数据库")

    if submit_btn:
        # 基础校验
        if not new_id or not new_name or not new_pi or not new_pwd:
            st.error("❌ 请填写所有带 * 的必填项！")
        else:
            # 清理动态组名中的空值
            valid_arms = [a.strip() for a in arms_inputs if a.strip()]
            valid_ratios = ratios_inputs[:len(valid_arms)]
            
            # 构建盲法配置字典
            blinds_map = generate_blinds_map(valid_arms, new_blind_level, new_seed)
            
            # 建立数据库会话并执行插入
            session = SessionLocal()
            try:
                # 核心解决办法：在 SQL 语句中对包含 json 字符串的参数显式追加 ::jsonb 转换
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
                    "t": new_id,
                    "name": new_name,
                    "pi": new_pi,
                    "blind": new_blind_level,
                    "pwd": new_pwd,
                    "strata": new_strata,
                    "arms": ",".join(valid_arms),
                    "ratios": ",".join(valid_ratios),
                    "seed": new_seed,
                    "blocks": json.dumps({}), # 纯文本转为标准 JSON 字符串
                    "blinds": json.dumps(blinds_map)
                })
                session.commit()
                st.success(f"🎉 项目 [{new_id}] 初始化成功！盲态设计：{new_blind_level}，已成功导入 PostgreSQL 数据库。")
            except Exception as e:
                session.rollback()
                st.error("数据库写入失败，捕获到原始错误详情：")
                st.exception(e)  # 直观暴露底层错误原因
            finally:
                session.close()

with tab2:
    st.header("已有项目列表与状态")
    
    session = SessionLocal()
    try:
        projects = session.execute(text("SELECT trial_id, trial_name, pi, blind_level, strata_list, arms FROM projects;")).fetchall()
        
        if not projects:
            st.info("💡 暂无已初始化的项目，请在左侧标签页中创建。")
        else:
            for proj in projects:
                with st.expander(f"📁 项目: {proj[0]} — {proj[1]}"):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("主要研究者 (PI)", proj[2])
                    c2.metric("盲态级别", proj[3])
                    c3.metric("研究组别 (Arms)", proj[5])
                    
                    st.markdown(f"**分层/中心列表:** `{proj[4]}`")
                    
                    # 模拟查看或调取该项目的随机化视图
                    if st.button("查看项目盲态快照 (需密码验证)", key=f"view_{proj[0]}"):
                        st.warning("请在生产环境中使用密码表单对 `blinds_config` 字段进行解密解破。")
    except Exception as e:
        st.error(f"无法读取项目列表: {e}")
    finally:
        session.close()
