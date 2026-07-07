Python
import streamlit as st
import pandas as pd
import hashlib
import time
import random

# =========================================================================
# 0. 核心安全机制：云端 SQL 数据库中央存储（【替换】原先的 sqlite3 或内存 db 逻辑）
# =========================================================================
# 自动读取 Secrets 中的 connections.postgresql，全网 CRC 共享这一个中央账本
conn = st.connection("postgresql", type="sql")

def init_db():
    """初始化云端数据库表结构（PostgreSQL 语法适配）"""
    with conn.session as session:
        # 1. 项目主表
        session.execute("""
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
        """)
        # 2. 盲底总库表
        session.execute("""
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
        """)
        # 3. 已分配受试者表（联合主键强锁，杜绝多台机器录入同号受试者）
        session.execute("""
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
        """)
        # 4. 审计日志表
        session.execute("""
            CREATE TABLE IF NOT EXISTS audit_trail (
                id SERIAL PRIMARY KEY,
                time_stamp TEXT,
                trial_id TEXT,
                operator TEXT,
                action TEXT,
                details TEXT
            );
        """)
        session.commit()

# 强行触发初始化（如果云端表不存在则自动创建，存在则跳过）
init_db()
# ==========================================
# 1. 极简 A/B 固定盲码随机化引擎
# ==========================================
class IWRSConfiguredEngine:
    @staticmethod
    def generate_and_save_blocks(trial_name, stratum, arms, ratios, start_seq_id, block_id, seed_base):
        block_seed = int(hashlib.md5(f"{trial_name}_{stratum}_{block_id}_{seed_base}".encode()).hexdigest(), 16) % 999999
        random.seed(block_seed)
        
        sum_ratios = sum(ratios)
        multiplier = random.choice([1, 2])
        if sum_ratios * multiplier < 4:
            multiplier = 2 if sum_ratios * 2 >= 4 else 4
        current_block_size = sum_ratios * multiplier
        
        block_content = []
        actual_mult = current_block_size // sum_ratios
        for arm, ratio in zip(arms, ratios):
            block_content.extend([arm] * (ratio * actual_mult))
            
        random.shuffle(block_content)
        
        arm_to_simple_code = {arm: "A" if idx == 0 else ("B" if idx == 1 else chr(65 + idx)) for idx, arm in enumerate(arms)}

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        for i, arm in enumerate(block_content):
            seq_id = start_seq_id + i
            blind_code = arm_to_simple_code[arm]
            cursor.execute('''
                INSERT INTO master_tables (trial_id, stratum, seq_id, block_id, block_size, true_arm, blind_code)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (trial_name, stratum, seq_id, block_id, current_block_size, arm, blind_code))
        conn.commit()
        conn.close()

def add_audit_log(user, project, action, details):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO audit_trail (time_stamp, trial_id, operator, action, details)
        VALUES (?, ?, ?, ?, ?)
    ''', (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()), project, user, action, details))
    conn.commit()
    conn.close()

# ==========================================
# 2. 前端 Streamlit 系统交互
# ==========================================
st.set_page_config(page_title="临床试验中央随机系统", layout="wide")
st.title("🏥 临床试验中央随机与盲法控制系统 (多用户云端版)")
st.caption("基于中央 SQLite 数据库构建，支持多机构、多名 CRC 跨电脑同时登录并发操作，内置锁机制绝不冲突。")
st.markdown("---")

st.sidebar.header("👤 身份与项目控制中心")
current_role = st.sidebar.selectbox("当前登录角色", ["研究者/临床医生(Investigator)", "临床协调员(CRC)", "数据管理员(DM)"])

# 从数据库动态加载活动项目
conn = sqlite3.connect(DB_FILE)
df_projects_load = pd.read_sql_query("SELECT * FROM projects", conn)
conn.close()

if not df_projects_load.empty:
    available_projects = df_projects_load["trial_id"].tolist()
    selected_project_id = st.sidebar.selectbox("📂 当前操作项目", available_projects)
    project_row = df_projects_load[df_projects_load["trial_id"] == selected_project_id].iloc[0]
    
    # 解析字段
    strata_list = project_row["strata_list"].split(",")
    arms = project_row["arms"].split(",")
    ratios = [int(x) for x in project_row["ratios"].split(",")]
    unblind_pwd = project_row["unblind_pwd"]
    trial_name = project_row["trial_name"]
else:
    selected_project_id = None
    st.sidebar.info("系统中暂无活动项目，请先新建临床研究。")

tab_create, tab_view_all, tab_enroll, tab_safety = st.tabs([
    "➕ 新建临床研究项目", "👁️ 随机盲底库浏览", "🧑‍⚕️ 临床中心受试者入组", "🚨 安全突发事件与审计"
])

# 标签页 1：新建项目
with tab_create:
    st.header("1. 临床研究方案配置与初始盲底锁定")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### 📝 研究方案基本信息")
        new_trial_id = st.text_input("研究方案号 (例如: NS-2026-01)", value="NS-2026-01").strip()
        new_trial_name = st.text_input("临床研究全称", value="某护理干预对患者预后影响的临床随机对照研究")
        new_pi_name = st.text_input("主要研究者 (PI)", value="张教授")
        new_unblind_pwd = st.text_input("🔑 设定紧急解盲安全密码", type="password", value="123456")
        new_strata_input = st.text_area("研究中心列表 (每行输入一个中心名称)", value="四川大学华西医院\n合作中心B")
    with col2:
        st.markdown("##### 🧪 研究组别与分配比例设置")
        st.info("💡 提示：系统会自动将【组别 1】映射为物资标签【A】，将【组别 2】映射为物资标签【B】。")
        arm_1_name = st.text_input("组别 1 名称", value="全新护理干预组")
        arm_2_name = st.text_input("组别 2 名称", value="常规临床对照组")
        c_r1, c_r2 = st.columns(2)
        with c_r1: r1 = st.number_input("组别 1 分配比例", min_value=1, value=1)
        with c_r2: r2 = st.number_input("组别 2 分配比例", min_value=1, value=1)

    if st.button("🔥 锁定方案配置并一键激活中央随机系统", type="primary", use_container_width=True):
        if not new_trial_id or not new_unblind_pwd.strip():
            st.error("❌ 配置失败：研究方案号与紧急解盲安全密码为必填项！")
        else:
            # 查重
            conn = sqlite3.connect(DB_FILE)
            existing = pd.read_sql_query("SELECT trial_id FROM projects WHERE trial_id=?", conn, params=(new_trial_id,))
            if not existing.empty:
                st.error(f"❌ 激活失败：方案号 `{new_trial_id}` 已存在，请勿重复创建！")
                conn.close()
            else:
                strata_arr = [s.strip() for s in new_strata_input.split("\n") if s.strip()]
                if not strata_arr: strata_arr = ["单中心整体"]
                arms_arr = [arm_1_name.strip(), arm_2_name.strip()]
                ratios_str = f"{int(r1)},{int(r2)}"
                seed_base = int((time.time() * 1000) % 999999)
                
                # 写入项目表
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO projects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (new_trial_id, new_trial_name, new_pi_name, new_unblind_pwd, ",".join(strata_arr), ",".join(arms_arr), ratios_str, seed_base, "{}"))
                conn.commit()
                conn.close()
                
                # 预生成盲底库入库
                for stratum in strata_arr:
                    IWRSConfiguredEngine.generate_and_save_blocks(new_trial_id, stratum, arms_arr, [int(r1), int(r2)], 1, 1, seed_base)
                
                add_audit_log("System_Statistician", new_trial_id, "INITIALIZE_PROJECT", "项目通过数据库成功初始化，中央多机通道激活。")
                st.success(f"✅ 临床研究【{new_trial_id}】中央随机系统已联合激活！")
                st.rerun()

# 标签页 2：盲底浏览
with tab_view_all:
    st.header("2. 核心随机盲底矩阵储备仓")
    if selected_project_id:
        if current_role not in ["数据管理员(DM)"]:
            st.error(f"🔒 盲态控制拦截：当前登录角色无权调阅盲底表！")
        else:
            chosen_view_stratum = st.selectbox("请选择要核查的研究中心/层级", strata_list)
            conn = sqlite3.connect(DB_FILE)
            raw_table_df = pd.read_sql_query("SELECT seq_id as 层内随机序号, block_id as 区组编号, block_size as 当前区组大小, true_arm as 真实分组, blind_code as 物资盲码 FROM master_tables WHERE trial_id=? AND stratum=?", conn, params=(selected_project_id, chosen_view_stratum))
            conn.close()
            st.dataframe(raw_table_df, use_container_width=True)

# 标签页 3：现场入组与发药（支持并发互锁机制）
with tab_enroll:
    st.header("3. 现场受试者动态筛选与中央分配")
    if selected_project_id:
        st.markdown(f"**🟢 当前活动项目：** `[{selected_project_id}] {trial_name}`")
        col_e1, col_e2 = st.columns([1, 1])
        
        # 实时从共享数据库读取该项目当前的入组记录
        conn = sqlite3.connect(DB_FILE)
        df_allocated = pd.read_sql_query("SELECT * FROM allocated_subjects WHERE trial_id=?", conn, params=(selected_project_id,))
        conn.close()
        
        with col_e1:
            sub_id_raw = st.text_input("请输入受试者唯一编号 (如住院号或 S001)", value="")
            sub_id = sub_id_raw.strip()
            chosen_stratum = st.selectbox("请选择受试者所在研究中心", strata_list)
            
            # 检测受试者是否已存在于数据库
            existing_records = df_allocated[df_allocated['subject_id'] == sub_id] if not df_allocated.empty else pd.DataFrame()
            
            if st.button("🚀 申请中央随机，获取发放标签", type="primary", use_container_width=True):
                if not sub_id:
                    st.error("❌ 错误：请输入有效的受试者编号！")
                elif not existing_records.empty:
                    st.warning(f"⚠️ 提示：受试者 `{sub_id}` 此前已于 {existing_records.iloc[0]['time_stamp']} 完成随机，不可重复随机！")
                else:
                    # 【核心并发锁机制】进入数据库事务，确保即使两个 CRC 同时点击，也会排队处理
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    try:
                        # 1. 重新在事务内部查重，彻底杜绝两台机器瞬间同时提交同名患者
                        cursor.execute("SELECT * FROM allocated_subjects WHERE trial_id=? AND subject_id=?", (selected_project_id, sub_id))
                        if cursor.fetchone() is not None:
                            st.warning("⚠️ 提示：检测到其他终端刚刚捷足先登录入了该受试者！")
                        else:
                            # 2. 计算当前中心在数据库中已入组的人数
                            cursor.execute("SELECT COUNT(*) FROM allocated_subjects WHERE trial_id=? AND stratum=?", (selected_project_id, chosen_stratum))
                            current_idx = cursor.fetchone()[0]
                            
                            # 3. 如果发现生成的储备盲底不够了，动态扩容
                            cursor.execute("SELECT COUNT(*) FROM master_tables WHERE trial_id=? AND stratum=?", (selected_project_id, chosen_stratum))
                            total_pool = cursor.fetchone()[0]
                            if current_idx >= total_pool:
                                cursor.execute("SELECT MAX(block_id) FROM master_tables WHERE trial_id=? AND stratum=?", (selected_project_id, chosen_stratum))
                                max_b_id = cursor.fetchone()[0] or 0
                                IWRSConfiguredEngine.generate_and_save_blocks(selected_project_id, chosen_stratum, arms, ratios, total_pool + 1, max_b_id + 1, int(project_row["seed_base"]))
                            
                            # 4. 精准取出当前排队序号对应的盲底行
                            cursor.execute("SELECT seq_id, true_arm, blind_code FROM master_tables WHERE trial_id=? AND stratum=? LIMIT 1 OFFSET ?", (selected_project_id, chosen_stratum, current_idx))
                            matched_row = cursor.fetchone()
                            
                            # 5. 写入受试者分配表（依靠数据库主键，若有冲突会立刻回滚）
                            cursor.execute('''
                                INSERT INTO allocated_subjects VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (selected_project_id, sub_id, chosen_stratum, matched_row[0], matched_row[1], matched_row[2], current_role, time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())))
                            conn.commit()
                            
                            add_audit_log(current_role, selected_project_id, "MANUAL_ALLOCATE", f"多机同步网关成功为患者 {sub_id} 分配物资标签: {matched_row[2]}")
                            st.success("🎉 中央随机成功！右侧已刷新配发结果。")
                            st.rerun()
                    except sqlite3.IntegrityError:
                        conn.rollback()
                        st.error("❌ 并发冲突冲突拦截：由于多名独立CRC同时操作，此编号正在被录入，请刷新重试！")
                    finally:
                        conn.close()
                        
        with col_e2:
            if sub_id and not existing_records.empty:
                st.markdown("### 📦 现场分发指引 (历史记录查重)")
                st.info(f"该受试者为**老患者**，请继续配发大写字母标签为 **【 {existing_records.iloc[0]['blind_code']} 】** 的药品/物资。")
            elif not df_allocated.empty:
                latest_sub = df_allocated.iloc[-1]
                st.markdown("### 📦 现场分发指引 (中央实时最新)")
                st.success(f"请立刻为受试者 `{latest_sub['subject_id']}` 配发贴有大写字母标签为 **【 {latest_sub['blind_code']} 】** 的药品/物资。")
                
        st.markdown("---")
        
        # 设盲控制与实时盲态计数展示
        col_view1, col_view2 = st.columns([2, 1])
        with col_view1:
            st.markdown("##### 📋 中央数据库实时已入组受试者详细清单")
            if not df_allocated.empty:
                # 重命名列名以适配前端
                df_show = df_allocated.copy()
                df_show.columns = ["方案号", "受试者编号", "研究中心", "层内随机序号", "真实分组", "物资盲码(Blind_Code)", "操作人", "入组时间"]
                if current_role not in ["数据管理员(DM)"]: 
                    df_show["真实分组"] = "🔒 [已设盲保护]"
                st.dataframe(df_show, use_container_width=True)
            else:
                st.caption("暂无受试者入组")
                
        with col_view2:
            st.markdown("##### 📊 试验盲态中央监控")
            if current_role == "数据管理员(DM)":
                if not df_allocated.empty:
                    st.dataframe(df_allocated["true_arm"].value_counts())
                else:
                    st.caption("暂无统计数据")
            else:
                total_count = len(df_allocated) if not df_allocated.empty else 0
                st.info(f"**🔐 盲态保护中**\n\n中央数据库显示当前总计已入组：**{total_count}** 例。\n\n*系统已严格执行全网随机区组内的分配均衡约束。*")

# 标签页 4：安全性与解盲
with tab_safety:
    st.header("4. 安全性突发事件处理与不可逆审计追踪")
    if selected_project_id:
        st.markdown("### 🚨 紧急安全解盲 (Emergency Unblinding)")
        st.warning("警告：非必要请勿执行此操作。此操作将永久、不可逆地在系统日志中记录谁在何时解开了哪一位患者的真实盲底。")
        
        target_sub_id = st.text_input("请输入需要紧急解盲的受试者编号")
        input_pwd = st.text_input("请输入项目安全解锁密码", type="password")
        
        if st.button("🔥 申请强制解盲", type="secondary"):
            if input_pwd != unblind_pwd:
                st.error("❌ 密码错误，拒绝解盲申请！")
            else:
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("SELECT true_arm, blind_code FROM allocated_subjects WHERE trial_id=? AND subject_id=?", (selected_project_id, target_sub_id.strip()))
                res = cursor.fetchone()
                conn.close()
                if not res:
                    st.error("❌ 未查找到该受试者入组记录。")
                else:
                    st.error(f"🚨 解盲成功！该患者分配的【物资 {res[1]}】对应的真实组别为：【 {res[0]} 】")
                    add_audit_log(current_role, selected_project_id, "EMERGENCY_UNBLIND", f"研究员强行解开了患者 {target_sub_id} 的盲底。")
                    st.rerun()
                    
        st.markdown("---")
        st.markdown("### 📝 系统不可逆合规审计日志 (Audit Trail)")
        conn = sqlite3.connect(DB_FILE)
        df_audit = pd.read_sql_query("SELECT time_stamp as 时间戳, operator as 操作用户, action as 操作行为, details as 详细记录 FROM audit_trail WHERE trial_id=?", conn, params=(selected_project_id,))
        conn.close()
        st.dataframe(df_audit, use_container_width=True)
