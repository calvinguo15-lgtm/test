import streamlit as st
import pandas as pd
import hashlib
import time
import random

# ==========================================
# 1. 工业级：多臂自适应无限扩容随机化引擎
# ==========================================
class IWRSConfiguredEngine:
    @staticmethod
    def generate_blind_block(trial_name, stratum, arms, ratios, start_seq_id, block_id, seed_base):
        """以单个区组为单位，动态追加生成高度安全的随机序列"""
        # 为当前区组动态生成独立种子，融合时钟、区组ID与方案号，保障不可预测性
        block_seed = int(hashlib.md5(f"{trial_name}_{stratum}_{block_id}_{seed_base}".encode()).hexdigest(), 16) % 999999
        random.seed(block_seed)
        
        sum_ratios = sum(ratios)
        # 智能区组长度控制：自适应交替使用1倍或2倍基础比例和，且确保不小于4，彻底打破猜盲规律
        multiplier = random.choice([1, 2])
        if sum_ratios * multiplier < 4:
            multiplier = 2 if sum_ratios * 2 >= 4 else 4
            
        current_block_size = sum_ratios * multiplier
        
        # 填充区组内容
        block_content = []
        actual_mult = current_block_size // sum_ratios
        for arm, ratio in zip(arms, ratios):
            block_content.extend([arm] * (ratio * actual_mult))
            
        # 充分打乱区组内顺序
        random.shuffle(block_content)
        
        # 组装盲底数据
        block_rows = []
        for i, arm in enumerate(block_content):
            seq_id = start_seq_id + i
            hash_input = f"{trial_name}_{stratum}_{seq_id}_{arm}_{block_seed}"
            blind_code = "MED-" + hashlib.md5(hash_input.encode()).hexdigest()[:8].upper()
            
            block_rows.append({
                "层内随机序号": seq_id,
                "区组编号(Block_ID)": block_id,
                "当前区组大小": current_block_size,
                "真实分组(True_Arm)": arm,
                "物资盲码(Blind_Code)": blind_code
            })
            
        return block_rows

# ==========================================
# 2. 系统全局状态与持久化模拟数据库
# ==========================================
st.set_page_config(page_title="临床试验中央随机与盲法控制系统", layout="wide", page_icon="🏥")

if "projects_db" not in st.session_state:
    st.session_state.projects_db = {}
if "audit_trail" not in st.session_state:
    st.session_state.audit_trail = []       

def add_audit_log(user, project, action, details):
    st.session_state.audit_trail.append({
        "时间戳": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
        "操作项目": project,
        "操作用户": user,
        "操作行为": action,
        "详细记录": details
    })

# ==========================================
# 3. 系统主体界面布局
# ==========================================
st.title("🏥 临床试验中央随机与盲法控制系统 (IWRS)")
st.caption("系统完全依从《药物临床试验质量管理规范(GCP)》及电子数据合规性技术指南规范")
st.markdown("---")

# 侧边栏：核心权限与活动项目切换
st.sidebar.header("👤 身份与项目控制中心")
current_role = st.sidebar.selectbox("当前登录角色", ["研究者/临床医生(Investigator)", "临床协调员(CRC)", "申办方监查员(CRA)", "数据管理员(DM)"])

st.sidebar.markdown("---")
if st.session_state.projects_db:
    available_projects = list(st.session_state.projects_db.keys())
    selected_project_id = st.sidebar.selectbox("📂 当前操作项目", available_projects)
    project_data = st.session_state.projects_db[selected_project_id]
else:
    selected_project_id = None
    project_data = None
    st.sidebar.info("系统中暂无活动项目，请先新建临床研究。")

# 系统四大核心功能模块页签
tab_create, tab_view_all, tab_enroll, tab_safety = st.tabs([
    "➕ 新建临床研究项目", 
    "👁️ 随机盲底库浏览", 
    "🧑‍⚕️ 临床中心受试者入组", 
    "🚨 安全突发事件与审计"
])

# ------------------------------------------
# 页签 1：新建临床研究项目（全新表单化、参数智能托管）
# ------------------------------------------
with tab_create:
    st.header("1. 临床研究方案配置与初始盲底锁定")
    
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("##### 📝 研究方案基本信息")
        new_trial_id = st.text_input("研究方案号 (首位唯一标识)", value="")
        new_trial_name = st.text_input("临床研究全称", value="")
        new_pi_name = st.text_input("主要研究者 (PI)", value="")
        new_blind_type = st.selectbox("盲法设计类型", ["第三方评价者盲法 (单盲态评估)", "双盲 (Double-Blind)", "开放/不设盲"])
        new_unblind_pwd = st.text_input("🔑 设定紧急解盲安全密码", type="password")
        
        st.markdown("##### 🏢 临床分层/研究中心设置")
        new_strata_input = st.text_area("研究中心列表 (每行输入一个中心名称)", value="四川大学华西口腔医院\n区域合作中心")

    with col2:
        st.markdown("##### 🧪 研究组别与分配比例设置")
        
        # 动态控制组别数量
        if "arm_count" not in st.session_state:
            st.session_state.arm_count = 2
            
        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("➕ 增加研究组别", use_container_width=True):
                st.session_state.arm_count += 1
        with btn_col2:
            if st.button("➖ 减少研究组别", use_container_width=True) and st.session_state.arm_count > 2:
                st.session_state.arm_count -= 1
        
        # 表单动态渲染独立的输入框
        arms_list = []
        ratios_list = []
        
        default_names = ["试验组", "对照组", "研究组C", "研究组D", "研究组E"]
        for idx in range(st.session_state.arm_count):
            c_arm, c_rat = st.columns([3, 1])
            with c_arm:
                d_name = default_names[idx] if idx < len(default_names) else f"研究组{chr(65+idx)}"
                arm_name = st.text_input(f"组别 {idx+1} 名称", value=d_name, key=f"arm_n_{idx}")
            with c_rat:
                arm_ratio = st.number_input(f"比例", min_value=1, value=1, step=1, key=f"arm_r_{idx}")
            arms_list.append(arm_name.strip())
            ratios_list.append(int(arm_ratio))

    st.markdown("---")
    if st.button("🔥 锁定方案配置并一键激活中央随机系统", type="primary", use_container_width=True):
        if not new_trial_id.strip() or not new_unblind_pwd.strip():
            st.error("❌ 配置失败：研究方案号与紧急解盲安全密码为必填项！")
        elif len(set(arms_list)) != len(arms_list):
            st.error("❌ 配置失败：各个研究组别名称不能完全相同！")
        elif new_trial_id in st.session_state.projects_db:
            st.error(f"❌ 配置失败：方案号【{new_trial_id}】在系统中已存在，请勿重复创建。")
        else:
            strata_list = [s.strip() for s in new_strata_input.split("\n") if s.strip()]
            if not strata_list:
                strata_list = ["单中心整体"]
                
            # 系统完全托管参数：随机初始化基础时钟种子
            seed_base = int((time.time() * 1000) % 999999)
            
            # 初始化盲底大容器，并自动生成首批区组（包含50例储备，后续智能无感扩容）
            master_tables = {}
            current_block_ids = {}
            
            for stratum in strata_list:
                master_tables[stratum] = []
                current_block_ids[stratum] = 1
                
                # 初始预生成足够基础消耗的区组储备
                while len(master_tables[stratum]) < 50:
                    start_id = len(master_tables[stratum]) + 1
                    b_rows = IWRSConfiguredEngine.generate_blind_block(
                        new_trial_id, stratum, arms_list, ratios_list, 
                        start_id, current_block_ids[stratum], seed_base
                    )
                    master_tables[stratum].extend(b_rows)
                    current_block_ids[stratum] += 1
            
            # 记录项目核心数据
            st.session_state.projects_db[new_trial_id] = {
                "trial_name": new_trial_name,
                "pi": new_pi_name,
                "blind_type": new_blind_type,
                "unblind_pwd": new_unblind_pwd,
                "strata_list": strata_list,
                "arms": arms_list,
                "ratios": ratios_list,
                "seed_base": seed_base,
                "current_block_ids": current_block_ids, # 追踪当前各层的区组ID进度
                "master_tables": master_tables,         # 基础储备盲底文件
                "allocated_subjects": [],               # 入组真实患者流水账
                "unblinded_list": set()                 # 破盲标记集
            }
            
            add_audit_log("Independent_Statistician", new_trial_id, "INITIALIZE_PROJECT", 
                          f"项目初始化成功。组别架构: {list(zip(arms_list, ratios_list))}。开启后台自适应区组及无限扩容引擎。")
            st.success(f"✅ 临床研究【{new_trial_id}】中央随机配置已成功激活！请在左侧边栏切换项目开始入组。")
            st.rerun()

    # 系统托管项目一览
    if st.session_state.projects_db:
        st.markdown("### 🗂️ 运行中临床研究项目清单")
        summary_data = []
        for pid, pdata in st.session_state.projects_db.items():
            summary_data.append({
                "方案号": pid,
                "临床研究名称": pdata["trial_name"],
                "主要研究者(PI)": pdata["pi"],
                "盲法设计": pdata["blind_type"],
                "包含中心/层级数": len(pdata["strata_list"]),
                "当前已入组受试者": f"{len(pdata['allocated_subjects'])} 例"
            })
        st.dataframe(pd.DataFrame(summary_data), use_container_width=True)

# ------------------------------------------
# 页签 2：盲底库浏览（展示系统智能分配的结果）
# ------------------------------------------
with tab_view_all:
    st.header("2. 核心随机盲底矩阵储备仓")
    if not selected_project_id:
        st.warning("⚠️ 请先创建或选择一个活动项目。")
    else:
        st.subheader(f"🗂️ 方案号：{selected_project_id}")
        
        if current_role not in ["数据管理员(DM)", "申办方监查员(CRA)"]:
            st.error(f"🔒 盲态控制拦截：当前登录身份【{current_role}】属于临床盲态执行人员，严禁调阅后台随机盲底表格！")
        else:
            st.success(f"🔓 身份验证成功：非盲权限【{current_role}】获准查看项目当前已生成的随机储备序列。")
            chosen_view_stratum = st.selectbox("请选择要核查的研究中心/层级", project_data["strata_list"])
            
            raw_table_df = pd.DataFrame(project_data["master_tables"][chosen_view_stratum])
            display_df = raw_table_df.copy()
            
            # CRA进行盲态监查时模糊治疗组，只有最高权限DM导出盲底时才可见真实分组
            if "开放" not in project_data["blind_type"] and current_role == "CRA":
                display_df["真实分组(True_Arm)"] = "🔒 [已设盲] 仅限非盲数据管理员(DM)导出盲底使用"
                
            st.dataframe(display_df, use_container_width=True)
            
            if current_role == "DM":
                csv_blind = raw_table_df.to_csv(index=False).encode('utf-8-sig')
                st.download_button(
                    label="📥 导出当前已生成全量盲底文件 (CSV)",
                    data=csv_blind,
                    file_name=f"IWRS_Master_Table_{selected_project_id}_{chosen_view_stratum}.csv",
                    mime='text/csv'
                )

# ------------------------------------------
# 页签 3：患者动态入组（包含全自动智能扩容逻辑）
# ------------------------------------------
with tab_enroll:
    st.header("3. 现场受试者动态筛选与中央分配")
    if not selected_project_id:
        st.warning("⚠️ 请在左侧控制中心选择一个活动项目。")
    else:
        st.markdown(f"**🟢 当前活动项目：** `[{selected_project_id}] {project_data['trial_name']}`")
        st.markdown(f"**研究涉及组别：** {', '.join(project_data['arms'])} | **设计盲法：** {project_data['blind_type']}")
        st.markdown("---")
        
        col_e1, col_e2 = st.columns(2)
        with col_e1:
            st.markdown("##### ✍️ 录入筛选合格受试者信息")
            sub_id = st.text_input("请输入受试者唯一编号 (例如: S001)", value="")
            chosen_stratum = st.selectbox("请选择受试者所在研究中心", project_data["strata_list"])
            
            if current_role in ["申办方监查员(CRA)", "数据管理员(DM)"]:
                st.error(f"❌ 权限拦截：当前角色【{current_role}】属于非盲/监查角色，无权执行临床现场分配操作！")
                allow_alloc = False
            else:
                allow_alloc = True
                
            if st.button("🚀 申请中央随机，动态匹配物资盲码", type="primary", disabled=not allow_alloc):
                if not sub_id.strip():
                    st.error("❌ 错误：受试者编号不能为空！")
                elif any(x['Subject_ID'] == sub_id.strip() for x in project_data["allocated_subjects"]):
                    st.error(f"❌ 错误：受试者编号 {sub_id} 已存在，不可重复分配。")
                else:
                    # 1. 计算当前中心已分配的人数
                    allocated_in_stratum = [x for x in project_data["allocated_subjects"] if x['Stratum'] == chosen_stratum]
                    current_idx = len(allocated_in_stratum)
                    
                    # 2. 【核心智能化：无感自动扩容技术】
                    # 如果当前已分配人数接近或超过了当前已生成的储备量，系统自动在后台静默生成全新的下一个区组
                    if current_idx >= len(project_data["master_tables"][chosen_stratum]) - 2:
                        start_id = len(project_data["master_tables"][chosen_stratum]) + 1
                        next_block_id = project_data["current_block_ids"][chosen_stratum]
                        
                        # 静默追加一个完美的、动态长度的新区组
                        new_block_rows = IWRSConfiguredEngine.generate_blind_block(
                            selected_project_id, chosen_stratum, project_data["arms"], project_data["ratios"],
                            start_id, next_block_id, project_data["seed_base"]
                        )
                        project_data["master_tables"][chosen_stratum].extend(new_block_rows)
                        project_data["current_block_ids"][chosen_stratum] += 1
                    
                    # 3. 提取分配的行数据
                    matched_row = project_data["master_tables"][chosen_stratum][current_idx]
                    
                    # 4. 写入临床随访数据库
                    project_data["allocated_subjects"].append({
                        "Subject_ID": sub_id.strip(),
                        "Stratum": chosen_stratum,
                        "Stratum_Seq_ID": matched_row["层内随机序号"],
                        "True_Arm": matched_row["真实分组(True_Arm)"],
                        "Blind_Code": matched_row["物资盲码(Blind_Code)"],
                        "Operator": current_role,
                        "Time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
                    })
                    
                    add_audit_log(current_role, selected_project_id, "ALLOCATE_SUBJECT", 
                                  f"受试者 {sub_id} 在层 [{chosen_stratum}] 成功执行分配，匹配序号: No.{matched_row['层内随机序号']}。")
                    st.success(f"🎉 动态中央随机分配成功！")
                    st.rerun()

        with col_e2:
            st.markdown("##### 💊 当期受试者动态响应结果")
            if project_data["allocated_subjects"]:
                latest = project_data["allocated_subjects"][-1]
                st.metric(label="当前中心已消耗序号至", value=f"No. {latest['Stratum_Seq_ID']}")
                st.code(f"现场必须指派/配发的物资盲码： {latest['Blind_Code']}", language="markdown")
                
                if "开放" not in project_data["blind_type"]:
                    st.warning("🔒 严格盲态提示：真实分组名称已屏蔽。请根据上述物资盲码配发带有对应标签的治疗耗材。")
                else:
                    st.success(f"🔓 开放标签提示：该受试者真实分入组别为 【{latest['True_Arm']}】")
            else:
                st.info("等待左侧录入筛选合格患者信息提交随机分配...")

        st.markdown("---")
        st.markdown(f"##### 📊 项目 [{selected_project_id}] 实时已入组临床随访流水明细")
        if project_data["allocated_subjects"]:
            df_alloc = pd.DataFrame(project_data["allocated_subjects"])
            
            # 根据角色和盲态动态隔离真实治疗组别
            if "开放" not in project_data["blind_type"] and current_role not in ["数据管理员(DM)"]:
                df_alloc["True_Arm"] = "🔒 [已设盲] 仅限非盲数据管理员(DM)可调阅"
                
            # 渲染已触发破盲的个例
            for idx, row in df_alloc.iterrows():
                if row["Subject_ID"] in project_data["unblinded_list"]:
                    df_alloc.at[idx, "True_Arm"] = f"🚨已紧急解盲: {project_data['master_tables'][row['Stratum']][row['Stratum_Seq_ID']-1]['真实分组(True_Arm)']}"

            st.dataframe(df_alloc[["Subject_ID", "Stratum", "Stratum_Seq_ID", "Blind_Code", "True_Arm", "Operator", "Time"]], use_container_width=True)

# ------------------------------------------
# 页签 4：安全性破盲与不可逆审计
# ------------------------------------------
with tab_safety:
    st.header("4. 安全性突发事件处理与不可逆审计追踪")
    if not selected_project_id:
        st.warning("⚠️ 请先创建或选择一个活动项目。")
    else:
        col_s1, col_s2 = st.columns([2, 3])
        with col_s1:
            st.subheader("🚨 严重不良事件(SAE)紧急医学解盲安全锁")
            st.markdown("⚠️ 警告：该操作属于最高级别合规事件，一旦执行将永久记入稽查追踪，且无法回滚。")
            
            input_sub = st.text_input("1. 请输入需紧急破盲的受试者编号", value="")
            input_reason = st.text_area("2. 请输入强制医学破盲的详细医学理由 (GCP必填核查项)", value="")
            input_sign = st.text_input("3. 临床主要研究者(PI)或执行医生电子签名确认", value="")
            input_pwd = st.text_input("🔑 4. 请输入该研究项目独属的紧急解盲密码锁", type="password")
            
            if st.button("🔥 验证双重安全锁并强制解盲", type="primary"):
                if not input_sub.strip() or not input_reason.strip() or not input_sign.strip() or not input_pwd.strip():
                    st.error("❌ 解盲失败：以上 4 项关键核查要素必须全部完整填写！")
                else:
                    if input_pwd.strip() != project_data["unblinded_pwd"]:
                        st.error("❌ 密码错误！解盲安全锁拒绝访问。该非法尝试已强制通报并写入安全审计轨迹。")
                        add_audit_log(f"Unauthenticated_User_{input_sign}", selected_project_id, "SECURITY_VIOLATION_ATTEMPT", 
                                      f"警告：有人企图使用错误密码对受试者 {input_sub} 实施恶意破盲！")
                    else:
                        found = [x for x in project_data["allocated_subjects"] if x['Subject_ID'] == input_sub.strip()]
                        if not found:
                            st.error(f"❌ 检索失败：在当前项目 [{selected_project_id}] 中未检索到编号为 {input_sub} 的受试者。")
                        else:
                            project_data["unblinded_list"].add(input_sub.strip())
                            true_treatment = found[0]["True_Arm"]
                            
                            add_audit_log(f"Investigator_{input_sign}", selected_project_id, "EMERGENCY_UNBLIND_LOGGED", 
                                          f"双重密码核验成功，强制执行医学破盲。破盲患者: {input_sub}。抢救医学事由: {input_reason}")
                            st.error(f"🚨 安全锁已完全解开，患者【{input_sub}】盲态完全刺破！")
                            st.info(f"该受试者在后台盲底矩阵中实际分入的是：【{true_treatment}】。请立即采取针对性临床抢救。")
                            st.rerun()

        with col_s2:
            st.subheader("🛡️ 系统不可逆合规审计追踪记录 (GCP Audit Trail)")
            if current_role not in ["申办方监查员(CRA)", "数据管理员(DM)"]:
                st.warning("🔒 依从性提示：前台盲态临床执行人员(研究者/CRC)无权调阅底层合规稽查大日志。请切换身份为 CRA 或 DM 调阅。")
            else:
                if st.session_state.audit_trail:
                    df_audit = pd.DataFrame(st.session_state.audit_trail)
                    filtered_audit = df_audit[df_audit["操作项目"] == selected_project_id]
                    st.dataframe(filtered_audit, use_container_width=True)
                    
                    csv_logs = filtered_audit.to_csv(index=False).encode('utf-8-sig')
                    st.download_button(
                        label="📥 导出当前项目合规审计追踪大日志 (CSV)",
                        data=csv_logs,
                        file_name=f"GCP_Audit_Trail_{selected_project_id}.csv",
                        mime='text/csv'
                    )