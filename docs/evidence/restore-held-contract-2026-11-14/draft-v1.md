# 受管备份导入、持久隔离与只读复核 · B 窄契约草案

**DRAFT_NOT_APPROVED · 2026-11-14 · 作者技术提案；不是实施授权或产品验收。**
固定输入：`9413c7ca66af56fce8ac1664e989cf8cd6fc9808`，tree `73558d71cbce08e8d5a86aa8fce26cd89f476d3b`。B的全部验收/断点为 **NOT_RUN**；PR144原生CI状态由root另核，本稿不更新。
下一步：独立第二读者先审§2–§7的写前保全、别名/实体锁与发布事实；通过后另交纯`docs(contract):` PR，再计划产品实现。

## 1. 用户结果与现行差异

完整备份经真实管理apply复制到原路径，发布后仍持久隔离；重启、pure_only、旧Run、后台及rebuild均不能进入普通业务栈。用户可专用只读review、同批resume、合法未发布abort。成功只表示“已导入、仍隔离、可复核”。
C切片在B交付后另审release、受管v2普通准入与完整旧Run恢复；B没有release事件/API，不默批`new_runs_only`，不结清PRD§8.5恢复服务义务。
现有`restore-plan`继续只读；当前公共`restore_backup`的直接copy/rename旁路必须收为受管私有复制能力，或让公共API进入本协议；仅改CLI不构成交付。
七DATA/SUM依赖继承fix `0785ae91ce75cd7a45201886423c5d54a18bf949`、integration `016910970d381a66ab12a94a5c88080a8cbf7437`及最小交付包；相同blob只绑定身份，受影响路径改变才重审差异，不重做另一debug线。

## 2. 支持输入与作者选型

只支持现有已校验完整备份、标准根`events.db`、保存的原canonical路径及其已核别名；不跨路径搬迁。源路径按备份保存的词法canonical值比较，不重新resolve已变化/不存在的活源。backup、target、staging、registry不得互为包含路径。
机制v0/合法legacy及v2可导入held；v1中间态、未来/坏机制、payload/记忆版本、缺失材料按真实解析器拒绝。v2备份不含progress，专用review只读观察其机制，既不伪造completed也不放行普通无progress v2。
`run.start` v0/v1/v2、原`event_versions`、原执行policy、三态结果与事件字节全部保留。B复制不补资格、不消费旧许可、不启动服务。
原缺/零main准入继续接受**任意数量全空骨架**；标准5项旁证、custom4项旁证及标准三材料域保持原语义。零main不截断、不重置；坏材料、链接、不可读或不该存在的progress在目标mkdir/anchor/SQLite connect前拒绝。
作者选择准备件备选A：registry完整链与既有库完整版本扫描在gate外；缺/零main的完整流式核空在shared gate内、写前完成。普通shared打开可并行，深空骨架可能延迟任意恢复reservation；复制不持gate。
这替换旧v2的128条目/短gate策略：不设新增条目、路径段/字节、ID数量/长度、marker或sealed尺寸政策上限，不承诺短gate、固定SLO或OS I/O截止。实际OS/内存/SQLite资源失败如实返回、现场保全，不把它叫输入损坏或截断后放行。
备选B（另加连续根范围锁、在gate外核空）暂不采用：它可改善reservation等待，但增加不存在文件的身份与全材料写者归属协议，本片先选更少的协作状态。此取舍经root允许写为提案，仍待独立审阅。

## 3. 权威、范围与实体身份

### 3.1 固定权威与不可变链

固定OS用户定位器从真实账户/已知Profile取得Home，使用`.tianshu/restore-registry-v1/registry.db`；不读cwd、HOME、TIANSHU_AUTHORITY_ROOT或产品参数来切换权威。首次在同父私有临时目录初始化/持久化，gate内仅原子发布完整权威；reserved持久提交前不动target/staging。
根内`.restore-origin.json`仅指向固定registry的registry_id/batch_id及sealed/binding摘要，不接受其任意路径为读取授权；一次写入、随根发布，不存可变released布尔值。只有registry完全不存在且无任何origin见证时，保留原生legacy路径。
固定registry目录已存在而DB/meta缺失、不可读、热journal无法只读解释、链/头不符、未知格式或marker单缺/错均闭锁，不新建空权威洗掉历史。registry与全部marker被同uid同时抹除不可区分legacy；不加全量bootstrap，不承诺防外部篡改。
registry有严格meta、只INSERT的控制事件及同事务CAS头缓存；头不能独立授状态。每事件含正整数sequence、batch_id、previous_sha256、kind、严格分型payload、完整规范字节SHA256；首previous为null，拒绝重复JSON字段/未知字段/版本/kind、bool冒整数及强转。完整快照核meta、全链、头的一致性；哈希不是同uid安全边界。
唯一状态链为`reserved → sealed_hold → publish_intent → published_hold`；只可从确定未进入publish_intent的reserved/sealed_hold追加`aborted`。重复同一事实返回原结果、不追加同义事件；每批各阶段唯一。B无released分支。
`reserved`绑定registry/batch、同一backup/manifest/双向清单摘要、plan、原目标absent/empty证明、最终root/DB的ScopeDescriptor、唯一同父staging名；`sealed_hold`绑定§5完整Binding；`publish_intent`引用原sealed、marker与最终位置；`published_hold`引用原intent/最终核对；`aborted`引用从未发布与清理证明。业务events不写这些控制事实，它们也不构成工具副作用许可。

### 3.2 ScopeDescriptor与别名

ScopeDescriptor记录canonical路径段及**从既有OS目录句柄取得的祖先目录身份链、最近既有祖先与未出现的后缀段**；数据根后来出现时，sealed再绑定实际根目录身份。原生大小写/路径比较复用pathkeys语义，不凭字符串前缀判包含，不加路径深度上限。
普通打开与reservation都重开并核祖先身份：沿共享既有目录身份映射剩余路径段，判相等/祖先/后代；published根还按已绑定根目录身份匹配从其他别名到达的后代。不存在文件也能按“稳定祖先实体＋后缀”识别同一位置。别名不能唯一对应、祖先置换或身份不可取得即拒绝/过期，不靠旧canonical值放行。
所有未aborted批次的final root和本批staging范围均保留，互相包含/相同、普通main落入任一范围或实体等于held DB都拒绝；`aborted`只解除已证明无恢复根的预约，不许可任何残留origin。普通custom.db/后代DB与标准main同受范围裁决。
恢复main及材料要求真实普通文件、无symlink/junction且main硬链接数为1；其OS实体身份从不截断的打开句柄取得，POSIX至少设备＋inode、Windows至少卷＋file identity，路径hash不是该身份。staging→final同卷rename必须保持文件/根实体身份；做不到即不支持apply。

## 4. 组合access与写前线性化

现有`DatabaseAccess`只按canonical路径hash加锁，**尚未提供实体身份锁**。本提案保留其namespace句柄，并增加OS文件身份键的shared/exclusive锁，组成同一连接持有的CompositeDatabaseAccess；不能把现有实现称作已闭合。
唯一共持取锁顺序：**restore gate → canonical namespace access → entity access（实体存在时）→ registry短读/事务**。全为非阻塞协作锁；registry busy_timeout=0。取锁忙、观察过期、取消/异常均释放本次临时资源返回可重试busy，不持锁sleep，不采用旧3次/250ms政策。
registry完整只读快照先在gate外完成并结束读事务，输出内部Observation（权威实体/完整已验证head/Scope决定）；不存在权威用明确absent-directory令牌。gate内只核同实体/meta/head及origin与祖先观察；过期回报busy，调用者重新完整观察，不用head代替全链。

### 4.1 普通打开（真实连接、后台和独立材料API共用）

1. gate外做完整registry观察与原T1只读证据预检，保存main absent/zero/nonempty及已存在实体/祖先身份；已held或坏现场在目标写前拒绝。`:memory:`仍独立，不创建文件权威/锁。
2. 取shared gate、canonical namespace shared。已有main先不截断打开真实句柄、取实体shared，并核句柄/路径/main/祖先一致；缺main此时没有entity锁，不伪造一个身份。核registry head与scope；历史非空库不套新空域遍历规则。
3. 缺/零main在仍持gate/namespace（zero另持entity）的情况下**重新完整流式核空和全部旁证**，再核main及祖先；任意坏/不可读、竞争创建或变化均写前返回。仅缺main在通过后才创建必要父目录并O_EXCL、不跟随最终链接地创建真实零anchor，立即从句柄取实体shared；如果实体锁/身份转交失败，保留新空现场并报失败，不初始化或把它当准入已过。
4. 既有zero不截断；创建期间普通竞争者共享namespace但O_EXCL只有一个胜者，败者释放临时资源、重新观察。已取得实体锁后重核main与全部新父目录身份；同canonical别名归namespace，同硬链接实体归entity，不靠basename锁。
5. 保持**同一namespace＋entity access**释放gate，gate外再核原T1证据并SQLite connect，核database_list.main/句柄实体/路径相同；在同一个只读快照完成真实schema/机制/全部payload版本准入。通过前不设WAL、不执行DDL、不建payload/Store；失败先关SQLite/实体句柄再释本次access。
6. 组合access交给真实连接，直到SQLite真正close成功才释放；cross-thread close失败仍活的连接继续持锁。不能在anchor→connect、完整检查→业务运行间释放后补取。

普通业务event append保持原事务入口；机制/progress/DB置换等维护持同namespace＋entity exclusive。三材料域每个写API须绑定所属**已初始化、已准入main**的连续access，不能拿custom.db锁写标准材料，不能在missing/zero未初始化时先造材料；不满足者首次写前拒绝或收口。
未持锁的raw SQLite、旧程序、同uid直接改文件属声明外边界；当前程序所有公开/后台/material/维护写者必须有入口归属索引和真实探针证明遵守，不能以“共有前缀”推定未接线API已覆盖。

### 4.2 新reservation与竞争结论

apply先在gate外完整观察/校验，取exclusive gate、最终标准DB namespace exclusive；若main已出现或目标非空，拒绝而不为预约造anchor。对既有empty root、所有已存在祖先/backup和registry核实体；同事务INSERT reserved＋head CAS，durable后才放gate。namespace exclusive贯穿apply；P2实际stage main出现后，在无SQLite/材料副作用执行前取其entity exclusive，并连续持到结束。
普通先行：gate内已固定真实anchor后才释gate；同DB的namespace/entity shared阻挡applyexclusive，custom/后代DB使真实root非空，apply也拒绝。apply先行：durable reserved后即使target/DB都不存在，普通scope裁决也拒绝。reserved后copy不持gate，无关根普通打开可前进。
恢复期间任何DB/根/材料维护都必须遵守同一实体/namespace归属与范围预约；当前namespace锁不能自行发现硬链接。实现必须新增实体锁及scope适配，任一OS无法可信取得/保持上述身份就拒绝受管apply；不能静默退回canonical路径证明。

## 5. 完整绑定、复制容量与P0–P5

BindingV1含registry/batch、最终canonical root/DB及根/DB实体身份、保存的源路径/已核别名、manifest/双向完整文件清单、关闭自包含DB字节、真实SQLite逻辑/机制/全材料及完整review scope的算法与SHA256、依赖身份。规范编码/域分隔与实际schema测试绑定；不靠计数、maxseq或manifest单向列表。
review scope覆盖全部记忆/正文/状态/来源、所有cursor/消息/原文、summary/来源、intent/result/Receipt/undo与snapshot闭包、审批/grant、全部Run/任务/claim/租约及Run引用并集。B用完整静态副本保留全部本体，不另造供旧/新Run成员执行判断的名单；C在允许写入前必须另定其不可变归属，不能把本稿的hash-only scope外推成C已经有名单本体。
完整材料沿现有三域逐项枚举，包括未引用范围文件；link、未知文件类型、根外引用/缺材料拒绝。origin及本批staging控制token另绑，非业务材料；progress、锁/socket、凭据、日志不复制，不回活源补内容。
容量选择是本片独立提案：采用普通逐字节复制；在staging所在真实卷比较可用字节与**尚需复制文件的已验证逻辑字节和**，不足/未知先拒绝。registry及目标父目录也分别实际探测，不借同卷或A的2GiB阈值跳过；元数据/journal/并发耗用不在字节和保证内，不能称空间预留。每次resume、开始/继续复制及发布前重核；所有真实写错误按所在阶段保全。

1. **P0只读预检**：真实verify、版本/同原路径、完整引用/材料、plan与依赖身份、布局及目标absent/empty、平台身份/durable能力和容量均通过。plan不是许可；apply重验，未reserved无target/staging改动。
2. **P1 durable reserved**：按§4.2发布必要权威并提交唯一预约，包含最终/staging ScopeDescriptor。reserved失败不开始copy；输出/错误都带batch_id，重开可从固定权威按root读回唯一活动批次。
3. **P2同父staging**：排他创建登记的本批目录，先写/持久化严格staging token（registry/batch/reserved摘要/目录实体），再copy；重启无token只允许核实完全空的本批预约目录后rmdir重建，有不明内容即保全。token后只能清理身份/token都匹配的本批stage，不能凭目录名字rmtree。逐项验证复制并形成完整Binding，stage DB非截断句柄＋entity exclusive全程持有。
4. **P3 sealed hold**：重核完整stage后durable追加sealed_hold，绑定自此不可改；原子写origin并核其字节，持久化复制文件/目录。token是独立控制见证，可随根保留但backup必须排除；丢失/变化不靠重封同batch洗白。
5. **P4 publish intent与发布**：完整重核stage、身份/材料、容量和目标仍原absent/empty；先durable追加publish_intent，再只rmdir原空root、同卷原子rename、持久化父目录。已有任何不明目标不删/不覆盖；intent后不再允许abort，即使本次未观察到rename。
6. **P5最终核对**：实际final main/root实体、origin、完整Binding都一致才durable追加published_hold并返回成功；只报held、batch、指纹及review命令。最终内容本体完整且与seal一致时，补published不依赖旧backup仍可访问；不复制新root重试、不生成业务事件/Receipt、不开服务。

## 6. 只读review与状态输出

建议显式入口：`data restore-apply BACKUP --data-root ROOT --expect-plan SHA256`、`restore-review --data-root ROOT --batch UUID [--later-backup B2]`、`restore-resume`、`restore-abort`；公共Python API走相同受管协议。具体拼写可在正式契约落库时对照现有parser，plan继续独立只读。
review只接受完整published_hold；按gate→namespace shared→entity shared核原观察，释放gate后持续access打开专用只读静态DB。全链扫描在gate外；固定权威/业务文件均只读，拒绝需要hot-journal恢复或会落临时文件的路径，不实例化EventLog/Store/Loop，不迁移/rebuild或改投影/registry，不调用模型/工具，不消耗审批/claim/租约。
输出完整Binding/scope指纹、全部检查与对象ID/状态/来源引用、未决三态/旧许可/租约、已知忘记/删除与不可核项；默认正文脱敏、哈希/大小可查，显式本地范围参数才显示正文。stdout可由用户保存在root外，工具自身不落报告文件。
较新后续史只接现有完整、已verify、同源路径的只读备份，逐项列出已知变化；坏/换输入使review_complete=false。无输入也明确未知；无论是否提供，`later_forget_delete_history=completeness_unproven`。已知忘记不能因恢复激活，所有普通业务仍held。
review无法穷尽任何必需范围就`review_complete=false`并非零退出；完整也只证明字节/引用/既有语义检查覆盖，不代表用户已人工读完正文，更不产release动作/许可。退出0仅当前管理动作完成；拒绝/失败2，真实crashpoint77；错误包含阶段、batch、现场与下一合法命令。
错误类别须在正式类型/schema/PRD同改并测：held、record_corrupt、binding_mismatch、incompatible、capacity_unavailable、durability_unavailable、maintenance_busy、restore_failed；未知异常保留原失败，不兜底为无历史/已abort。

## 7. resume、abort与durable边界

resume仅按同batch原backup/plan与不可变链推进：reserved未seal可在token/身份匹配的stage继续或受控重建；seal后内容变化就保全报错，不重封；origin未完成只能补同一绑定。publish_intent后若final已匹配，幂等核对补published；若stage仍匹配且final仍原缺/空，继续既定发布；两处都无对应实体、两份矛盾或身份不明即保全闭锁，不按新库/新batch绕过。
abort先在无publish_intent的事实下安全清理本批stage，再以exclusive gate→namespace/entity exclusive→registry顺序重核target仍原缺/空、无本批origin/不明材料及完整链，最后durable追加aborted；不删registry历史。任何可能已发布、身份不明或清理失败缺省拒绝；不得用abort把已发布隔离根变普通根。
reserved以前失败只有无目标写或私有权威初始化残留；reserved以后失败持续held。rename已经/可能发生但P5未完必须报告“发布收尾未完成、仍隔离”，不能声称原目标未动。ENOSPC/I/O/权限/取消/损坏不无条件删stage或final。
平台适配必须显式核registry初始化/事务提交、token/origin原子写、文件/目录持久化和同卷rename语义；现有可能吞fsync错误的helper不能直接充当证明。能力不足返回durability_unavailable并保全，不能静默跳过后报durable。
本契约要求的是声明OS/文件系统原语保证下的崩溃可解释性；真实os._exit(77)只证明进程硬退，物理断电、外部设备缓存失效另需证据。原生runner证据按实际系统/架构绑定，不外推用户Windows10/11、ARM64、桌面或全文件系统支持。

## 8. 独立验收草案（全部NOT_RUN）

| B项 | 真实行为/必红负控 | 旧RA映射及变更 |
|---|---|---|
| B01 | 两真实进程屏障覆盖普通先anchor/apply先reserved，缺root重开无第二空历史；删前缀或提前释access应红 | RA01–04、37；加实体别名/缺实体转换 |
| B02 | custom/后代/可解析别名/同实体硬链接、后台/心跳、pure_only、rebuild及独立材料API held拒绝；旁路变异应红 | RA06–07、10、13；全入口索引 |
| B03 | missing/zero坏旁证/材料在零anchor写前拒绝；大于128任意空骨架仍可过；真非空原库原准入保持 | RA43重订；不继承128/129拒绝 |
| B04 | 长核空shared gate下普通并行、reservation busy；copy期间无关根可前进；锁/异常close连续性 | RA05、35–37、42重订；不验旧短锁/时限/3次 |
| B05 | 权威/marker单缺、坏链/head/未知格式、实体替换、scope别名、首权威竞争均拒绝；无见证合法legacy正控 | RA08–12、41–42；双缺边界单列 |
| B06 | 改manifest/实际DB/投影/FTS/Receipt/summary来源/未引用材料/原policy均拒绝，真实schema与七依赖身份 | RA14、21、23、29–31；依赖不变者精确继承 |
| B07 | held review全逻辑/控制/材料前后字节与集合不变，零模型/工具/消费；后续忘记变化列全且不宣称完整 | RA15–16；不用release期望 |
| B08 | 真CLI apply→独立进程仍held→review，同batch重开/断点resume幂等；只能合法未发布abort | RA24–25、27–28；新增publish_intent守卫 |
| B09 | 每实际位置容量不足/未知与copy/commit/origin/rename阶段ENOSPC/I/O均保全，能力不足明确拒绝 | RA27–28；不借2GiB，不伪造空间预留 |

真实断点映射：P0无目标写；FP-INIT、FP-RESERVED；P2的FP-STAGE及token原子写；P3的FP-SEALED、FP-MARKER（tmp-write/fsync/rename/dir-fsync）；P4新增FP-PUBLISH-INTENT提交前/后及FP-RMDIR/FP-RENAME；P5的FP-PUBLISHED；另FP-ABORT事务前/后。逐点os._exit(77)、重开普通负探针与两次幂等收尾；全部NOT_RUN。
RA17–20、22、26中的release段、32/38/39与旧new_runs_only期望留C重订；RA33/34旧Run集合/固定资源上限不复制进B。RA40只对账B不冒称完整服务恢复。旧43项与10断点原件保持历史NOT_RUN，不把本表数量相加当新验收数。

## 9. 待第二读者裁定与正式提交边界

本稿已选择A和publish_intent，仍需第二读者独立审：祖先身份＋后缀的scope等价算法能否覆盖所声明别名；namespace→entity转换是否全程无写前历史丢失；已存在main/硬链接/缺文件的每个获取分支；token空窗、publish_intent与abort是否可恢复且不误删。
OS文件身份/目录身份与durable适配、全部写者归属索引及真实schema是**待实施证明义务**，当前没有这些产品证据；若某平台无法实现§3–§4的具体顺序，该平台apply明确不支持，不能写成已复用maintenance解决。若独立读者发现描述不能确定实现，先修契约，不让作者以实现细节名义绕过。
正式PR只写B契约和必要PRD/KN状态，不改产品；正文写原直接restore→受管held→原因→受影响入口。产品实施另计划/PR，按AGENTS独立复审及四required，不借本稿或PR144绿色冒充B通过。
下一动作：独立读者按B01/B03/B05/B08的反例尝试打破§3–§7，并返回具体行号、最短时序与契约已修/部分/未修分类。
