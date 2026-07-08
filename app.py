import streamlit as st
import pandas as pd
import hashlib
import time
import random
import json
# 引入 SQLAlchemy 的 text 函数以适配云端多数据库环境
from sqlalchemy import text

# =========================================================================
# 0. 核心安全机制：云端 SQL 数据库中央存储（通过 pg8000 驱动连接 Neon）
# =========================================================================
st.set_page_config(page_title="临床试验多项目中央随机化与盲底管理系统", layout="wide")
conn = st.connection("postgresql", type="sql")

def init_db():
    """初始化云端中央数据库表结构（适配 SQLAlchemy 2.0+ 语法）"""
    with conn.session as session:
        # 1. 项目主表（增加 blinds_config 存储自定义盲态设置）
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS projects (
                trial_id TEXT PRIMARY KEY,
                trial_name TEXT,
                pi TEXT,
                unblind_pwd TEXT,
                strata_list TEXT,
                arms TEXT,
                ratios TEXT,
                seed_base INTEGER,
                current_block_ids TEXT,
                blinds_config TEXT
            );
        """))
        
        # 2. 盲底总库表
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS master_tables (
                id SERIAL PRIMARY KEY,
                trial_id TEXT,
                stratum TEXT,
                seq_id INTEGER,
                block_id INTEGER,
                block_size INTEGER,
                true_arm TEXT,
                blind_code TEXT
            );
        """))
        
        # 3. 已分配受试者表
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS allocated_subjects (
                trial_id TEXT,
                subject_id TEXT,
                stratum TEXT,
                stratum_seq_id INTEGER,
                true_arm TEXT,
                blind_code TEXT,
                operator TEXT,
                time_stamp TEXT,
                PRIMARY KEY (trial_id, subject_id)
            );
        """))
        
        # 4. 审计日志与紧急揭盲记录表
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS audit_trail (
                id SERIAL PRIMARY KEY,
                time_stamp TEXT,
                trial_id TEXT,
                operator TEXT,
                action TEXT,
                details TEXT
            );
        """))
        session.commit()

# 自动运行数据库初始化
init_db()

# =========================================================================
# 1. 核心随机化引擎（动态区块生成）
# =========================================================================
def generate_block(trial_id, stratum, block_id, block_size, arm_list, ratio_list, seed_base):
    """基于伪随机种子的确定性区块生成函数"""
    seed_str = f"{trial_id}_{stratum}_{block_id}_{seed_base}"
    seed = int(hashlib.sha256(seed_str.encode('utf-8')).hexdigest()[:8], 16)
    rng = random.Random(seed)
    
    total_ratio = sum(ratio_list)
    multipliers = block_size // total_ratio
    
    pool = []
    for arm, ratio in zip(arm_list, ratio_list):
        pool.extend([arm] * (ratio * multipliers))
    
    rng.shuffle(pool)
    return pool

# =========================================================================
# 2. 数据库读取与写入工具函数
# =========================================================================
def get_all_projects():
    df = conn.query("SELECT * FROM projects;", ttl=0)
    return df

def get_allocated_count(trial_id, stratum):
    query = text("SELECT COUNT(*) FROM allocated_subjects WHERE trial_id = :t AND stratum = :s;")
    with conn.session as session:
        res = session.execute(query, {"t": trial_id, "s": stratum}).fetchone()
    return int(res[0]) if res else 0

def get_next_blind_info(trial_id, stratum, seq_id):
    """从盲底表读取当前序号对应的盲底信息，如果不存在则动态扩充区块"""
    query = text("SELECT true_arm, blind_code, block_id, block_size FROM master_tables WHERE trial_id = :t AND stratum = :s AND seq_id = :seq;")
    with conn.session as session:
        res = session.execute(query, {"t": trial_id, "s": stratum, "seq": seq_id}).fetchone()
    
    if res is not None:
        return {"true_arm": res[0], "blind_code": res[1], "block_id": res[2], "block_size": res[3]}
    
    # 盲底不足，触发动态扩充
    p_query = text("SELECT * FROM projects WHERE trial_id = :t;")
    with conn.session as session:
        p_res = session.execute(p_query, {"t": trial_id}).fetchone()
    
    if p_res is None:
        return None
    
    arm_list = [x.strip() for x in p_res[5].split(',')]
    ratio_list = [int(x.strip()) for x in p_res[6].split(',')]
    seed_base = p_res[7]
    current_block_ids_str = p_res[8]
    blinds_config_str = p_res[9] if len(p_res) > 9 else "{}"
    
    try:
        block_status = json.loads(current_block_ids_str)
    except:
        block_status = {}
        
    try:
        blinds_map = json.loads(blinds_config_str)
    except:
        blinds_map = {}
        
    current_block_id = block_status.get(stratum, 0)
    next_block_id = current_block_id + 1
    
    total_ratio = sum(ratio_list)
    block_size = total_ratio * 4 if total_ratio * 4 <= 12 else total_ratio * 2
    
    # 生成新区块
    new_arms = generate_block(trial_id, stratum, next_block_id, block_size, arm_list, ratio_list, seed_base)
    
    start_seq = (next_block_id - 1) * block_size + 1
    with conn.session as session:
        for i, arm in enumerate(new_arms):
            current_seq = start_seq + i
            # 如果项目自定义了盲态映射（如 试验组->A/C/E），则优先选用自定义，否则走默认
            if arm in blinds_map and blinds_map[arm]:
                b_code_options = [x.strip() for x in blinds_map[arm].split(',')]
                b_code = random.choice(b_code_options) # 区块内按配置映射
            else:
                b_code = "A" if arm == arm_list[0] else "B"
            
            session.execute(text("""
                INSERT INTO master_tables (trial_id, stratum, seq_id, block_id, block_size, true_arm, blind_code)
                VALUES (:t, :s, :seq, :bid, :bsize, :arm, :bcode);
            """), {"t": trial_id, "s": stratum, "seq": current_seq, "bid": next_block_id, "bsize": block_size, "arm": arm, "bcode": b_code})
        
        block_status[stratum] = next_block_id
        session.execute(text("UPDATE projects SET current_block_ids = :status WHERE trial_id = :t;"), 
                        {"status": json.dumps(block_status), "t": trial_id})
        session.commit()
        
    return get_next_blind_info(trial_id, stratum, seq_id)

# =========================================================================
# 3. SIDEBAR 左侧控制面板（人员角色选择与当前项目激活）
# =========================================================================
st.sidebar.title("🔐 中央安全访问控制")
user_role = st.sidebar.radio(
    "当前登录人员角色选择 (Role):",
    ["临床研究医生 (Investigator)", "项目申办方/监查员 (CRA/Sponsor)", "数据管理员 (DM/Biostatistician)", "系统高级管理员 (Admin)"],
    help="根据 GCP 规范，系统将基于角色对敏感数据（如真实组别、盲底结构）进行严格的动态权限隔离展示。"
)

df_p = get_all_projects()

st.sidebar.markdown("---")
st.sidebar.subheader("📂 活跃试验项目选择")
if df_p.empty:
    st.sidebar.info("系统中暂无可用项目，请先在中央面板中新建。")
    active_trial_id = None
else:
    project_options = {f"[{row['trial_id']}] {row['trial_name']}": row['trial_id'] for _, row in df_p.iterrows()}
    selected_proj_label = st.sidebar.selectbox("选择当前操作的项目：", list(project_options.keys()))
    active_trial_id = project_options[selected_proj_label]
    proj_data = df_p[df_p['trial_id'] == active_trial_id].iloc[0]

# =========================================================================
# 4. Streamlit 主界面模块布局
# =========================================================================
st.title("🧪 临床试验多项目中央随机化与盲底管理系统")
st.caption("基于安全隔离的确定性动态区块引擎 · 全流程审计追踪")

tabs = st.tabs(["📊 项目工作台", "➕ 新建研究项目", "🔐 密码中心与高级设置", "📜 审计日志调阅"])

# -------------------------------------------------------------------------
# TAB 1: 项目工作台（入组随机化与项目总览）
# -------------------------------------------------------------------------
with tabs[0]:
    if not active_trial_id:
        st.info("👋 欢迎使用中央系统！当前没有正在进行的研究项目，请先前往『新建研究项目』选项卡进行配置。")
    else:
        st.header(f"📋 当前项目：{proj_data['trial_name']}")
        st.markdown(f"**项目方案编号 (ID):** `{active_trial_id}` | **主要研究者 (PI):** `{proj_data['pi']}`")
        
        strata_options = [x.strip() for x in proj_data['strata_list'].split(',')]
        col1, col2 = st.columns([1, 2])
        
        with col1:
            st.subheader("👤 受试者入组登记")
            # 严格按照 GCP 规范限制：只有临床医生或管理员才能执行入组登记
            if user_role not in ["临床研究医生 (Investigator)", "系统高级管理员 (Admin)"]:
                st.warning("⚠️ 权限阻断：当前角色仅具备查看与质控权限，无法执行受试者入组随机化操作。")
            else:
                with st.form("allocation_form", clear_on_submit=True):
                    sub_id = st.text_input("受试者筛选号/随机号 (Subject ID):", placeholder="例如：SUB-001").strip()
                    selected_stratum = st.selectbox("分层因素组合 (Stratum):", strata_options)
                    operator = st.text_input("随访研究医生签名 (Operator):", value=user_role.split(" ")[0]).strip()
                    submit_alloc = st.form_submit_button("申请中央分组与获取盲码")
                    
                    if submit_alloc:
                        if not sub_id or not operator:
                            st.error("❌ 错误：受试者筛选号以及操作医生签名均不能为空！")
                        else:
                            # 检查受试者 ID 是否重复（安全无缓存会话执行）
                            with conn.session as session:
                                dup_check = session.execute(
                                    text("SELECT 1 FROM allocated_subjects WHERE trial_id = :t AND subject_id = :s;"), 
                                    {"t": active_trial_id, "s": sub_id}
                                ).fetchone()

                            if dup_check is not None:
                                st.error(f"❌ 错误：受试者号 '{sub_id}' 在本项目中已被分配，请勿重复提交。")
                            else:
                                current_cnt = get_allocated_count(active_trial_id, selected_stratum)
                                next_seq_id = current_cnt + 1
                                blind_info = get_next_blind_info(active_trial_id, selected_stratum, next_seq_id)
                                
                                if blind_info:
                                    time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                                    with conn.session as session:
                                        session.execute(text("""
                                            INSERT INTO allocated_subjects (trial_id, subject_id, stratum, stratum_seq_id, true_arm, blind_code, operator, time_stamp)
                                            VALUES (:t, :s, :stratum, :seq, :arm, :bcode, :op, :tm);
                                        """), {"t": active_trial_id, "s": sub_id, "stratum": selected_stratum, "seq": next_seq_id, 
                                               "arm": blind_info['true_arm'], "bcode": blind_info['blind_code'], "op": operator, "tm": time_now})
                                        
                                        log_details = f"Subject {sub_id} allocated to Blind Code: {blind_info['blind_code']} inside Stratum [{selected_stratum}] (Seq: {next_seq_id})"
                                        session.execute(text("""
                                            INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
                                            VALUES (:tm, :t, :op, 'SUBJECT_ALLOCATION', :det);
                                        """), {"tm": time_now, "t": active_trial_id, "op": operator, "det": log_details})
                                        session.commit()
                                    
                                    st.balloons()
                                    st.success(f"🎉 随机化分组成功！")
                                    st.metric(label="📊 获配药物/试验盲码 (Blind Code)", value=blind_info['blind_code'])
                                    st.info(f"分层序列号: {selected_stratum} - 第 {next_seq_id} 例")
                                else:
                                    st.error("❌ 引擎生成异常，请联系系统管理员。")
        
        with col2:
            st.subheader("📈 本项目入组动态看板")
            # 根据人员角色动态确定脱盲展示级别
            if user_role in ["系统高级管理员 (Admin)", "数据管理员 (DM/Biostatistician)"]:
                # 高级权限可以看到真实试验组别 true_arm
                df_alloc = conn.query("SELECT subject_id, stratum, stratum_seq_id, blind_code, true_arm, operator, time_stamp FROM allocated_subjects WHERE trial_id = :t ORDER BY time_stamp DESC;", 
                                      params={"t": active_trial_id}, ttl=0)
            else:
                # 临床医生和CRA严格双盲，只能看到盲码 blind_code
                df_alloc = conn.query("SELECT subject_id, stratum, stratum_seq_id, blind_code, operator, time_stamp FROM allocated_subjects WHERE trial_id = :t ORDER BY time_stamp DESC;", 
                                      params={"t": active_trial_id}, ttl=0)
            
            if df_alloc.empty:
                st.write("当前项目暂无受试者入组。")
            else:
                st.dataframe(df_alloc, use_container_width=True)
                
                st.markdown("**各分层因素入组例数分布统计：**")
                stats = []
                for st_name in strata_options:
                    cnt = len(df_alloc[df_alloc['stratum'] == st_name])
                    stats.append({"分层因子组合 (Stratum)": st_name, "已入组例数 (N)": cnt})
                st.table(pd.DataFrame(stats))

# -------------------------------------------------------------------------
# TAB 2: 新建研究项目（支持自定义盲态设置）
# -------------------------------------------------------------------------
with tabs[1]:
    st.header("新建多中心临床研究项目")
    st.caption("支持在此定义复杂的多盲态映射配置。")
    
    with st.form("new_project_form"):
        new_id = st.text_input("临床试验方案编号/试验 ID (Trial ID):", placeholder="例如：PROT-2026-001").strip()
        new_name = st.text_input("临床试验完整名称 (Trial Full Name):", placeholder="例如：某创新药随机双盲多中心试验")
        new_pi = st.text_input("主要研究者 (PI):", placeholder="张教授")
        
        st.markdown("---")
        new_strata = st.text_area("分层因子配置 (以英文逗号分隔):", 
                                  value="中心A-轻度, 中心A-重度, 中心B-轻度, 中心B-重度")
        
        col_a, col_b = st.columns(2)
        with col_a:
            new_arms = st.text_input("试验组别名称 (逗号分隔):", value="试验组, 安慰剂组")
        with col_b:
            new_ratios = st.text_input("分配比例 (逗号分隔):", value="1, 1")
            
        st.markdown("##### 🎭 高级盲态控制（自定义盲码映射）")
        st.caption("如需一个组别对应多个外观完全一致的包装代码以加强盲态，请在下方配置。留空则系统默认分配 A / B 码。")
        
        custom_blind_1 = st.text_input("为【第一个组别】映射的自定义盲码（多个用逗号隔开，留空则默认为 A）:", placeholder="例如：A, C, E")
        custom_blind_2 = st.text_input("为【第二个组别】映射的自定义盲码（多个用逗号隔开，留空则默认为 B）:", placeholder="例如：B, D, F")
        
        new_seed = st.number_input("项目随机基础种子偏移量 (Seed Base):", min_value=1000, max_value=999999, value=8888, step=1)
        new_pwd = st.text_input("本项目专属『紧急破盲密码』:", type="password")
        
        submit_proj = st.form_submit_button("🚀 批准并初始化该研究项目")
        
        if submit_proj:
            if not new_id or not new_name or not new_pwd:
                st.error("❌ 错误：核心字段不能为空！")
            else:
                with conn.session as session:
                    check_exist = session.execute(
                        text("SELECT 1 FROM projects WHERE trial_id = :t;"), 
                        {"t": new_id}
                    ).fetchone()

                if check_exist is not None:
                    st.error(f"❌ 冲突：系统内已存在编号为 '{new_id}' 的项目。")
                else:
                    # 组装自定义盲态字典
                    arms_parsed = [x.strip() for x in new_arms.split(',')]
                    blinds_map = {}
                    if len(arms_parsed) >= 1 and custom_blind_1:
                        blinds_map[arms_parsed[0]] = custom_blind_1
                    if len(arms_parsed) >= 2 and custom_blind_2:
                        blinds_map[arms_parsed[1]] = custom_blind_2
                        
                    time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    
                    with conn.session as session:
                        session.execute(text("""
                            INSERT INTO projects (trial_id, trial_name, pi, unblind_pwd, strata_list, arms, ratios, seed_base, current_block_ids, blinds_config)
                            VALUES (:t, :name, :pi, :pwd, :strata, :arms, :ratios, :seed, :blocks, :blinds);
                        """), {"t": new_id, "name": new_name, "pi": new_pi, "pwd": new_pwd, "strata": new_strata, 
                               "arms": new_arms, "ratios": new_ratios, "seed": new_seed, "blocks": json.dumps({}), "blinds": json.dumps(blinds_map)})
                        
                        session.execute(text("""
                            INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
                            VALUES (:tm, :t, 'SYSTEM_ADMIN', 'PROJECT_CREATION', :det);
                        """), {"tm": time_now, "t": new_id, "det": f"Project [{new_name}] created. Custom blinds configured: {json.dumps(blinds_map)}"})
                        session.commit()
                        
                    st.success(f"🎯 研究项目 [{new_id}] 初始化成功！已自动加载左侧项目看板。")
                    st.rerun()

# -------------------------------------------------------------------------
# TAB 3: 密码中心与高级设置（SAE 紧急揭盲与数据导出）
# -------------------------------------------------------------------------
with tabs[2]:
    st.header("🚨 紧急揭盲中心与项目数据安全导出")
    if not active_trial_id:
        st.info("暂无可用项目。")
    else:
        st.subheader("🔴 严重不良事件 (SAE) 紧急单中心一键破盲")
        st.warning("⚠️ 安全警告：紧急揭盲操作将永久记录在系统无法篡改的审计追踪表中！")
        
        target_sub_id = st.text_input("请输入需要破盲的受试者筛选号/随机号 (Subject ID):").strip()
        entered_pwd = st.text_input("请输入该项目专属的『紧急破盲密码』:", type="password")
        unblind_operator = st.text_input("执行破盲的授权医生/监查员姓名签名:", value=user_role.split(" ")[0]).strip()
        
        if st.button("🔥 确认执行紧急破盲并调阅真实组别", type="primary"):
            if not target_sub_id or not entered_pwd or not unblind_operator:
                st.error("❌ 所有输入项均为必填项！")
            elif entered_pwd != proj_data['unblind_pwd']:
                st.error("❌ 密码错误！紧急破盲请求已被阻断，操作已记入安全审计日志。")
                time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                with conn.session as session:
                    session.execute(text("""
                        INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
                        VALUES (:tm, :t, :op, 'UNBLIND_FAILED', :det);
                    """), {"tm": time_now, "t": active_trial_id, "op": unblind_operator, "det": f"Failed unblind attempt for Subject {target_sub_id}: Invalid password."})
                    session.commit()
            else:
                res_query = text("SELECT true_arm, blind_code, stratum, stratum_seq_id FROM allocated_subjects WHERE trial_id = :t AND subject_id = :s;")
                with conn.session as session:
                    subject_info = session.execute(res_query, {"t": active_trial_id, "s": target_sub_id}).fetchone()
                
                if subject_info is None:
                    st.error(f"❌ 未找到受试者号 '{target_sub_id}' 在项目中的入组记录。")
                else:
                    time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    with conn.session as session:
                        log_det = f"EMERGENCY UNBLIND COMPLETED for Subject {target_sub_id}. Exposed Arm: {subject_info[0]}."
                        session.execute(text("""
                            INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
                            VALUES (:tm, :t, :op, 'EMERGENCY_UNBLIND', :det);
                        """), {"tm": time_now, "t": active_trial_id, "op": unblind_operator, "det": log_det})
                        session.commit()
                    
                    st.error("🚨 紧急揭盲成功 —— 组别解密结果如下：")
                    st.write(f"**受试者筛选号**: `{target_sub_id}`")
                    st.markdown(f"### **底层真实分配组别 (True Treatment Group): :red[{subject_info[0]}]**")
                    st.info("此项揭盲记录已同步永久固化归档至审计追踪日志中。")
                    
        st.markdown("---")
        st.subheader("💾 导出项目数据报表")
        export_pwd = st.text_input("请输入解盲密码以核验导出权限:", type="password", key="export_pwd")
        if st.button("📥 生成数据表"):
            if export_pwd == proj_data['unblind_pwd']:
                export_df = conn.query("SELECT subject_id, stratum, stratum_seq_id, blind_code, operator, time_stamp FROM allocated_subjects WHERE trial_id = :t ORDER BY stratum, stratum_seq_id;", 
                                       params={"t": active_trial_id}, ttl=0)
                if export_df.empty:
                    st.warning("当前项目尚未录入任何受试者数据。")
                else:
                    st.dataframe(export_df)
                    csv = export_df.to_csv(index=False).encode('utf-8')
                    st.download_button("点击下载 CSV 报表", data=csv, file_name=f"Trial_{active_trial_id}_export.csv", mime="text/csv")
            else:
                st.error("权限核验失败：密码不正确。")

# -------------------------------------------------------------------------
# TAB 4: 审计日志调阅（全功能追溯核查）
# -------------------------------------------------------------------------
with tabs[3]:
    st.header("📜 全流程不可篡改临床安全审计追踪表 (Audit Trail)")
    df_logs = conn.query("SELECT time_stamp, trial_id, operator, action, details FROM audit_trail ORDER BY time_stamp DESC;", ttl=0)
    
    if df_logs.empty:
        st.write("当前中央系统日志记录库为空。")
    else:
        st.dataframe(df_logs, use_container_width=True)
        csv_logs = df_logs.to_csv(index=False).encode('utf-8')
        st.download_button("📥 导出全量系统审计日志", data=csv_logs, file_name="System_Central_Audit_Trail.csv", mime="text/csv")
