"""Learning content, exposed by account settings; all creative actions use existing APIs."""
STEPS = [
    ('goal', '选择创作起点', '确定这次要呈现的故事或参考。', '你提供的故事、想法或参考视频。', '明确从想法创作或参考复刻。', 'creation'),
    ('project', '建立自己的作品', '把素材、分镜和版本放在同一作品里。', '系列名称和作品标题。教学练习可命名为“教学小样（我的素材）”。', '当前账号下出现独立作品，已有作品不会被覆盖。', 'creation'),
    ('preset', '选择制作方案', '按创作需要选择当前可用的能力。', '先看方案的画幅、声音及素材要求。', '作品保存所选方案；这一步不会生成。', 'shots'),
    ('materials', '准备素材', '给模型提供这套方案实际需要的输入。', '从你的电脑选择图片、声音或参考视频；使用自己的或有权使用的素材。', '素材出现在当前作品；参考视频仍是素材，不是已生成分镜。', 'materials'),
    ('shots', '保存分镜', '将故事拆成能明确表演的片段。', '写提示词、按剧情设置时长，并绑定方案需要的素材。', '分镜保存成功，实际帧数与输出时长可在预检核对。', 'shots'),
    ('preflight', '先做无生成预检', '一次看清缺少的输入与版本问题。', '保存分镜，再点击预检；也可验证尚未保存的参数草稿。', '返回准备度和补齐入口。通过只证明可执行，不证明画质。', 'shots'),
    ('queue', '明确提交并排队', '将已冻结的请求交给共享资源队列。', '检查参数后，主动点击生成。忙时等待，重复请求标识可查询原回执。', '得到稳定任务回执、等待原因与排位；关闭页面不丢任务。', 'render'),
    ('review', '审核候选与重做', '决定是否采用这一版画面和声音。', '打开候选试听观看，保存问题；不满意时修改分镜并主动重做。', '当前分镜候选均审核通过；仅保存修改意见不代表完成，历史版本保留。', 'shots'),
    ('joins', '检查相邻接点', '发现动作、构图和环境声在切换处的变化。', '逐个看听前后片段与成片切点，写连续性约束并审核。', '当前版本的接点均审核通过；要求重做尚未完成，视频版本变化会使结论过期。', 'render'),
    ('sound', '选择完整音轨', '让全片声音形成连续的表达。', '保留各段原音，或上传并选择已包含对白、音乐和音效的完整母版。', '完整母版替换各段原音；请核对长度，预检不会自动装配。', 'render'),
    ('assembly', '装配并通过成片审核', '把已审核片段和选定音轨汇成候选，再检查全片。', '先看装配预检，再主动提交；候选生成后检查接点、声音与音画对齐，明确保存审核结论。', '当前成片候选审核通过才算完成；仅生成候选或标记“已了解”不代表审核通过。', 'render'),
    ('export', '导出交付', '保存已审核的作品与创作记录。', '审核成片，确认要交付的版本，再从现有入口取得文件。', '核对下载到自己电脑的文件。当前未记录下载交付回执，审核通过只说明可以导出。', 'render'),
]


def steps(readiness=None, *, has_project=False):
    readiness = readiness or {}
    segments = readiness.get('segments', [])
    requirements = readiness.get('input_requirements', [])
    assembly_state = readiness.get('assembly', {}).get('state')
    join_review = readiness.get('assembly', {}).get('preflight', {}).get('join_review') or {}
    material_labels = list(dict.fromkeys(item['label'] for item in requirements if item.get('kind') in {'图片', '音频', '视频'} and item.get('required')))
    completed = {'project': has_project, 'preset': bool(readiness.get('installed_stages')),
                 'shots': bool(segments), 'materials': bool(segments) and all(row.get('validation', {}).get('valid') is True for row in segments),
                 'preflight': bool(segments) and all(row.get('validation', {}).get('valid') is True for row in segments),
                 'queue': bool(segments) and any(row.get('task_id') or row.get('state') in {'pending_review', 'adopted', 'redo_required'} for row in segments),
                 'review': bool(segments) and all(row.get('state') == 'adopted' for row in segments),
                 'joins': join_review.get('completed') is True,
                 'assembly': assembly_state == 'adopted'}
    rows = [dict(id=key, title=title, purpose=purpose, prepare=prepare, result=result,
                 action=key, applicable=(len(segments) > 1 if key == 'joins' and has_project else True),
                 completed=bool(completed.get(key)))
            for key, title, purpose, prepare, result, action in STEPS]
    if readiness.get('creation_mode') == 'auto':
        preset_step = next(item for item in rows if item['id'] == 'preset')
        preset_step.update(title='描述你想要的画面', applicable=False, completed=False,
                           prepare='填写描述、剧情时长和目标画幅；开始画面与表演声音按需展开。',
                           state_label='制作方式由已保存的明确输入决定，无需选择或安装工作流。')
    join_step = next(item for item in rows if item['id'] == 'joins')
    join_state = join_review.get('state', 'unavailable' if len(segments) > 1 else 'not_applicable')
    join_step.update(state=join_state, state_label={
        'video_required': '请先生成接点两侧的当前视频，再查看并审核',
        'pending': '接点两侧已有视频，待逐个查看并审核',
        'approved': '当前版本的相邻接点均已审核通过',
        'redo_left': '接点前段需要重做，审核尚未完成',
        'redo_right': '接点后段需要重做，审核尚未完成',
        'stale': '接点视频版本已变化，请重新核对',
        'not_applicable': '当前没有相邻接点，无需接点审核',
        'unavailable': '请先读取接点状态并完成装配预检',
    }.get(join_state, '接点尚未审核完成'))
    assembly_step = next(item for item in rows if item['id'] == 'assembly')
    assembly_step.update(state=assembly_state, state_label={
        'pending_review': '候选已生成，待审核',
        'adopted': '当前成片审核已通过',
        'redo_required': '成片需修改后重做',
        'review_stale': '成片审核已过期，需重新核对',
    }.get(assembly_state, '成片尚未审核完成'))
    export_step = next(item for item in rows if item['id'] == 'export')
    # Existing export/download routes have no persisted delivery receipt.
    # Eligibility must not be presented as a completed download.
    export_step.update(available=assembly_state == 'adopted', state_label=(
        '审核通过，可以导出；尚未确认下载交付' if assembly_state == 'adopted' else
        '请先通过当前成片审核，再导出交付'))
    if requirements:
        row = next(item for item in rows if item['id'] == 'materials')
        row['prepare'] = ('当前方案需要：' + '、'.join(material_labels) + '。从你的电脑选择文件。') if material_labels else '当前方案无需外部媒体输入；先准备提示词。也可上传创作参考。'
        if not material_labels:
            row.update(applicable=False, completed=False, state='not_applicable',
                       state_label='当前方案从文字开始，无需首尾帧或外部音频；创作参考可选。')
    return rows
