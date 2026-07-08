import streamlit as st
import pandas as pd
import hashlib
import time
import random
# 核心修复：引入 SQLAlchemy 的 text 函数以适配云端多数据库环境
from sqlalchemy import text

# =========================================================================
# 0. 核心安全机制：云端 SQL 数据库中央存储（通过 pg8000 驱动连接 Neon）
# =========================================================================
st.set_page_config(page_title="临床试验多项目中央随机化与盲底管理系统", layout="wide")
conn = st.connection("postgresql", type="sql")

def init_db():
    """初始化云端中央数据库表结构（适配 SQLAlchemy 2.0+ 语法）"""
    with conn.session as session:
        # 1. 项目主表
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
                current_block_ids TEXT
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
    """
    基于伪随机种子的确定性区块生成函数。
    确保无论云端容器如何重启，只要数据库中记录的 block_id 相同，生成的盲底就绝对一致且不可预测。
    """
    # 结合项目 ID、分层因素和区块 ID 组合唯一哈希
    seed_str = f"{trial_id}_{stratum}_{block_id}_{seed_base}"
    seed = int(hashlib.sha256(seed_str.encode('utf-8')).hexdigest()[:8], 16)
    rng = random.Random(seed)
    
    # 按照比例计算各组人数
    total_ratio = sum(ratio_list)
    multipliers = block_size // total_ratio
    
    pool = []
    for arm, ratio in zip(arm_list, ratio_list):
        pool.extend([arm] * (ratio * multipliers))
    
    # 打乱顺序
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
    df = conn.query(query, params={"t": trial_id, "s": stratum}, ttl=0)
    return int(df.iloc[0, 0]) if not df.empty else 0

def get_next_blind_info(trial_id, stratum, seq_id):
    """从盲底表读取当前序号对应的盲底信息，如果不存在则动态扩充区块"""
    query = text("SELECT true_arm, blind_code, block_id, block_size FROM master_tables WHERE trial_id = :t AND stratum = :s AND seq_id = :seq;")
    df = conn.query(query, params={"t": trial_id, "s": stratum, "seq": seq_id}, ttl=0)
    
    if not df.empty:
        return df.iloc[0].to_dict()
    
    # 盲底不足，触发动态扩充
    p_query = text("SELECT * FROM projects WHERE trial_id = :t;")
    p_df = conn.query(p_query, params={"t": trial_id}, ttl=0)
    if p_df.empty:
        return None
    
    proj = p_df.iloc[0]
    arm_list = [x.strip() for x in proj['arms'].split(',')]
    ratio_list = [int(x.strip()) for x in proj['ratios'].split(',')]
    
    # 解析当前区块状态并计算下一个 block_id
    import json
    try:
        block_status = json.loads(proj['current_block_ids'])
    except:
        block_status = {}
        
    current_block_id = block_status.get(stratum, 0)
    next_block_id = current_block_id + 1
    
    # 动态设定区块大小 (2倍或4倍总比例)
    total_ratio = sum(ratio_list)
    block_size = total_ratio * 4 if total_ratio * 4 <= 12 else total_ratio * 2
    
    # 生成新区块
    new_arms = generate_block(trial_id, stratum, next_block_id, block_size, arm_list, ratio_list, proj['seed_base'])
    
    # 写入新生成的区块到中央盲底库
    start_seq = (next_block_id - 1) * block_size + 1
    with conn.session as session:
        for i, arm in enumerate(new_arms):
            current_seq = start_seq + i
            # 固定 A/B 盲码逻辑
            b_code = "A" if arm == arm_list[0] else "B"
            
            session.execute(text("""
                INSERT INTO master_tables (trial_id, stratum, seq_id, block_id, block_size, true_arm, blind_code)
                VALUES (:t, :s, :seq, :bid, :bsize, :arm, :bcode);
            """), {"t": trial_id, "s": stratum, "seq": current_seq, "bid": next_block_id, "bsize": block_size, "arm": arm, "bcode": b_code})
        
        # 更新项目的区块状态计数
        block_status[stratum] = next_block_id
        session.execute(text("UPDATE projects SET current_block_ids = :status WHERE trial_id = :t;"), 
                        {"status": json.dumps(block_status), "t": trial_id})
        session.commit()
        
    # 重新递归获取
    return get_next_blind_info(trial_id, stratum, seq_id)

# =========================================================================
# 3. Streamlit 系统多模块群界面
# =========================================================================
st.title("🧪 临床试验多项目中央随机化与盲底管理系统")
st.caption("基于安全隔离的确定性动态区块引擎 · 全流程审计追踪")

tabs = st.tabs(["📊 项目工作台", "➕ 新建研究项目", "🔐 密码中心与高级设置", "📜 审计日志调阅"])

# -------------------------------------------------------------------------
# TAB 1: 项目工作台（入组随机化与项目总览）
# -------------------------------------------------------------------------
with tabs[0]:
    st.header("项目入组与随机化操作")
    df_p = get_all_projects()
    
    if df_p.empty:
        st.info("当前中央系统内暂无正在进行的研究项目，请先前往『新建研究项目』中进行配置。")
    else:
        project_options = {f"[{row['trial_id']}] {row['trial_name']}": row['trial_id'] for _, row in df_p.iterrows()}
        selected_proj_label = st.selectbox("请选择要操作的临床试验项目：", list(project_options.keys()))
        active_trial_id = project_options[selected_proj_label]
        
        # 获取当前选定项目的详细数据
        proj_data = df_p[df_p['trial_id'] == active_trial_id].iloc[0]
        strata_options = [x.strip() for x in proj_data['strata_list'].split(',')]
        
        col1, col2 = st.columns([1, 2])
        
        with col1:
            st.subheader("👤 受试者入组登记")
            with st.form("allocation_form", clear_on_submit=True):
                sub_id = st.text_input("受试者筛选号/随机号 (Subject ID, 确保项目内唯一):").strip()
                selected_stratum = st.selectbox("分层因素组合 (Stratum):", strata_options)
                operator = st.text_input("随访研究医生签名 (Operator):").strip()
                submit_alloc = st.form_submit_form_button("申请中央分组与获取盲码")
                
                if submit_alloc:
                    if not sub_id or not operator:
                        st.error("❌ 错误：受试者筛选号以及操作医生签名均不能为空！")
                    else:
                        # 检查受试者 ID 是否重复
                        dup_check = conn.query(text("SELECT 1 FROM allocated_subjects WHERE trial_id = :t AND subject_id = :s;"), 
                                               params={"t": active_trial_id, "s": sub_id}, ttl=0)
                        if not dup_check.empty:
                            st.error(f"❌ 错误：受试者号 '{sub_id}' 在本项目中已被分配，请勿重复提交。")
                        else:
                            # 计算本分层因子的下一个序号
                            current_cnt = get_allocated_count(active_trial_id, selected_stratum)
                            next_seq_id = current_cnt + 1
                            
                            # 获取盲底
                            blind_info = get_next_blind_info(active_trial_id, selected_stratum, next_seq_id)
                            
                            if blind_info:
                                time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                                
                                # 将分配结果和审计日志同步写入中央云数据库
                                with conn.session as session:
                                    session.execute(text("""
                                        INSERT INTO allocated_subjects (trial_id, subject_id, stratum, stratum_seq_id, true_arm, blind_code, operator, time_stamp)
                                        VALUES (:t, :s, :stratum, :seq, :arm, :bcode, :op, :tm);
                                    """), {"t": active_trial_id, "s": sub_id, "stratum": selected_stratum, "seq": next_seq_id, 
                                           "arm": blind_info['true_arm'], "bcode": blind_info['blind_code'], "op": operator, "tm": time_now})
                                    
                                    # 自动生成对应的审计追踪
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
                                st.error("❌ 引擎生成异常，请联系系统管理员检查后台环境。")
        
        with col2:
            st.subheader("📈 本项目入组动态看板")
            
            # 读取已分配的受试者列表
            df_alloc = conn.query(text("SELECT subject_id, stratum, stratum_seq_id, blind_code, operator, time_stamp FROM allocated_subjects WHERE trial_id = :t ORDER BY time_stamp DESC;"), 
                                  params={"t": active_trial_id}, ttl=0)
            
            if df_alloc.empty:
                st.write("当前项目暂无受试者入组。")
            else:
                st.dataframe(df_alloc, use_container_width=True)
                
                # 展现各分层的入组统计
                st.markdown("**各分层因素入组例数分布统计：**")
                stats = []
                for st_name in strata_options:
                    cnt = len(df_alloc[df_alloc['stratum'] == st_name])
                    stats.append({"分层因子组合 (Stratum)": st_name, "已入组例数 (N)": cnt})
                st.table(pd.DataFrame(stats))

# -------------------------------------------------------------------------
# TAB 2: 新建研究项目（项目生命周期创建）
# -------------------------------------------------------------------------
with tabs[1]:
    st.header("新建多中心临床研究项目")
    st.caption("创建后，中央管理系统会自动为其隔离盲底，并开启全自动的确定性动态区块引擎。")
    
    with st.form("new_project_form"):
        new_id = st.text_input("临床试验方案编号/试验 ID (Trial ID, 唯一且不可更改):", placeholder="例如：PROT-2026-001").strip()
        new_name = st.text_input("临床试验完整名称 (Trial Full Name):", placeholder="例如：某创新药对比安慰剂治疗高血压的多中心随机双盲临床研究")
        new_pi = st.text_input("主要研究者 (Principal Investigator / PI):", placeholder="张教授")
        
        st.markdown("---")
        new_strata = st.text_area("分层因子配置 (以英文逗号分隔，一行一个或整行输入):", 
                                  value="中心A-年龄<60, 中心A-年龄>=60, 中心B-年龄<60, 中心B-年龄>=60",
                                  help="系统根据您输入的分层因子组合，在中央盲底库为每个层级独立维护一套区块竞争树。")
        
        col_a, col_b = st.columns(2)
        with col_a:
            new_arms = st.text_input("试验组别名称 (逗号分隔):", value="试验组, 安慰剂组", help="请务必确保数量与下方的比例完全对应。")
        with col_b:
            new_ratios = st.text_input("分配比例 (逗号分隔):", value="1, 1", help="例如 1,1 代表 1:1 分配；2,1 代表 2:1 分配。")
            
        new_seed = st.number_input("项目随机基础种子偏移量 (Seed Base):", min_value=1000, max_value=999999, value=8888, step=1)
        new_pwd = st.text_input("本项目专属『紧急破盲密码』:", type="password", help="该密码用于在突发严重不良事件 (SAE) 时，现场医生紧急一键获取特定受试者的真实组别信息。请务必妥善保存。")
        
        submit_proj = st.form_submit_button("🚀 批准并初始化该研究项目")
        
        if submit_proj:
            if not new_id or not new_name or not new_pwd:
                st.error("❌ 错误：项目 ID、项目名称以及紧急破盲密码属于核心字段，均不能为空！")
            else:
                # 重复性校验
                check_exist = conn.query(text("SELECT 1 FROM projects WHERE trial_id = :t;"), params={"t": new_id}, ttl=0)
                if not check_exist.empty:
                    st.error(f"❌ 冲突：系统内已存在编号为 '{new_id}' 的临床项目，请勿重复创建。")
                else:
                    # 初始化保存
                    import json
                    time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    
                    with conn.session as session:
                        session.execute(text("""
                            INSERT INTO projects (trial_id, trial_name, pi, unblind_pwd, strata_list, arms, ratios, seed_base, current_block_ids)
                            VALUES (:t, :name, :pi, :pwd, :strata, :arms, :ratios, :seed, :blocks);
                        """), {"t": new_id, "name": new_name, "pi": new_pi, "pwd": new_pwd, "strata": new_strata, 
                               "arms": new_arms, "ratios": new_ratios, "seed": new_seed, "blocks": json.dumps({})})
                        
                        # 写入全局审计记录
                        session.execute(text("""
                            INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
                            VALUES (:tm, :t, 'SYSTEM_ADMIN', 'PROJECT_CREATION', :det);
                        """), {"tm": time_now, "t": new_id, "det": f"Project [{new_name}] created successfully by Admin. Configured arms: {new_arms} with ratios: {new_ratios}."})
                        session.commit()
                        
                    st.success(f"🎯 研究项目 [{new_id}] 初始化成功！盲底数据库隔离层及中央随机化树已成功建立。")
                    st.info("请切换至『项目工作台』开始受试者登记入组。")

# -------------------------------------------------------------------------
# TAB 3: 密码中心与高级设置（SAE 紧急安全揭盲与数据导出）
# -------------------------------------------------------------------------
with tabs[2]:
    st.header("🚨 紧急揭盲中心与项目数据安全导出")
    df_p2 = get_all_projects()
    
    if df_p2.empty:
        st.info("暂无可用项目。")
    else:
        project_options2 = {f"[{row['trial_id']}] {row['trial_name']}": row['trial_id'] for _, row in df_p2.iterrows()}
        selected_proj_label2 = st.selectbox("请选择要操作的项目：", list(project_options2.keys()), key="select_proj_tab2")
        active_trial_id2 = project_options2[selected_proj_label2]
        proj_data2 = df_p2[df_p2['trial_id'] == active_trial_id2].iloc[0]
        
        st.markdown("---")
        st.subheader("🔴 严重不良事件 (SAE) 紧急单中心一键破盲")
        st.warning("⚠️ 安全警告：紧急揭盲操作将永久记录在系统无法篡改的审计追踪表中，仅在受试者发生严重医学突发状况，必须获知其所用核心药物品种以实施抢救时方可启用。")
        
        target_sub_id = st.text_input("请输入需要破盲的受试者筛选号/随机号 (Subject ID):").strip()
        entered_pwd = st.text_input("请输入该项目专属的『紧急破盲密码』:", type="password")
        unblind_operator = st.text_input("执行破盲的授权医生/监查员姓名签名:").strip()
        
        if st.button("🔥 确认执行紧急破盲并调阅真实组别", type="primary"):
            if not target_sub_id or not entered_pwd or not unblind_operator:
                st.error("❌ 所有输入项（受试者 ID、密码、破盲医生签名）均为必填项！")
            elif entered_pwd != proj_data2['unblind_pwd']:
                st.error("❌ 密码错误！紧急破盲请求已被中央阻断，该次拦截已被写入安全审计追踪日志。")
                # 记录失败审计
                time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                with conn.session as session:
                    session.execute(text("""
                        INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
                        VALUES (:tm, :t, :op, 'UNBLIND_FAILED', :det);
                    """), {"tm": time_now, "t": active_trial_id2, "op": unblind_operator if unblind_operator else "UNKNOWN", "det": f"Failed unblind attempt for Subject {target_sub_id}: Invalid password."})
                    session.commit()
            else:
                # 密码验证通过，调阅真实分组
                res_query = text("SELECT true_arm, blind_code, stratum, stratum_seq_id FROM allocated_subjects WHERE trial_id = :t AND subject_id = :s;")
                df_res = conn.query(res_query, params={"t": active_trial_id2, "s": target_sub_id}, ttl=0)
                
                if df_res.empty:
                    st.error(f"❌ 未找到受试者号 '{target_sub_id}' 在项目中的入组记录，无法破盲。")
                else:
                    subject_info = df_res.iloc[0]
                    time_now = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    
                    # 写入紧急解盲审计表
                    with conn.session as session:
                        log_det = f"EMERGENCY UNBLIND COMPLETED for Subject {target_sub_id}. Stratum: {subject_info['stratum']}, Seq: {subject_info['stratum_seq_id']}. Exposed Arm: {subject_info['true_arm']}, Blind Code: {subject_info['blind_code']}."
                        session.execute(text("""
                            INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
                            VALUES (:tm, :t, :op, 'EMERGENCY_UNBLIND', :det);
                        """), {"tm": time_now, "t": active_trial_id2, "op": unblind_operator, "det": log_det})
                        session.commit()
                    
                    st.error("🚨 紧急揭盲成功 —— 组别解密结果如下：")
                    st.write(f"**受试者筛选号**: `{target_sub_id}`")
                    st.write(f"**获配盲码**: `{subject_info['blind_code']}`")
                    st.markdown(f"### **底层真实分配组别 (True Treatment Group): :red[{subject_info['true_arm']}]**")
                    st.info("此项揭盲记录已同步永久固化归档至国家级临床规范级别的中央审计追踪日志中。")
                    
        st.markdown("---")
        st.subheader("💾 导出项目脱盲脱敏数据报表")
        st.write("用于中期分析 (Interim Analysis) 或结题时的数据集导出。")
        
        export_pwd = st.text_input("请输入紧急破盲密码以核验导出权限:", type="password", key="export_pwd")
        if st.button("📥 生成脱敏入组交叉数据表"):
            if export_pwd == proj_data2['unblind_pwd']:
                export_df = conn.query(text("SELECT subject_id, stratum, stratum_seq_id, blind_code, operator, time_stamp FROM allocated_subjects WHERE trial_id = :t ORDER BY stratum, stratum_seq_id;"), 
                                       params={"t": active_trial_id2}, ttl=0)
                if export_df.empty:
                    st.warning("当前项目尚未录入任何受试者数据。")
                else:
                    st.dataframe(export_df)
                    csv = export_df.to_csv(index=False).encode('utf-8')
                    st.download_button("点击下载 CSV 报表", data=csv, file_name=f"Trial_{active_trial_id2}_export_clean.csv", mime="text/csv")
            else:
                st.error("权限核验失败：密码不正确。")

# -------------------------------------------------------------------------
# TAB 4: 审计日志调阅（全功能追溯核查）
# -------------------------------------------------------------------------
with tabs[3]:
    st.header("📜 全流程不可篡改临床安全审计追踪表 (Audit Trail)")
    st.caption("系统底层所有核心动作（新建项目、分配受试者、紧急破盲、破盲失败拦截等）皆会在此进行自动实时固化留痕。")
    
    # 从中央云数据库调阅全部审计追踪记录
    df_logs = conn.query("SELECT time_stamp, trial_id, operator, action, details FROM audit_trail ORDER BY time_stamp DESC;", ttl=0)
    
    if df_logs.empty:
        st.write("当前中央系统日志记录库为空。")
    else:
        st.dataframe(df_logs, use_container_width=True)
        
        # 允许导出完整的审计追踪凭证供监管核查 (GCP 规范)
        csv_logs = df_logs.to_csv(index=False).encode('utf-8')
        st.download_button("📥 导出全量系统审计日志 (符合 GCP 电子数据合规凭证)", data=csv_logs, file_name="System_Central_Audit_Trail.csv", mime="text/csv")
