# 受管备份导入、持久隔离与只读复核 · B 契约

**契约文字已独立复核，产品尚未实现。** 2026-11-14；全部 B 验收与断点仍为 **NOT_RUN**。
代码基线为 main `8f2a508e68f27efc4100b7466fcd79043341f7fa`，tree `73558d71cbce08e8d5a86aa8fce26cd89f476d3b`；与文字审阅固定的 `9413c7ca66af56fce8ac1664e989cf8cd6fc9808` 同 tree。
[v3 独立文字复审](evidence/restore-held-contract-2026-11-14/review-v3.md)结论仅为 `PASS_FOR_PURE_DOCS_PR_TEXT_ONLY`。§1–§8 保留已审 v3 规范正文；原草案、前两轮未通过报告和逐字节映射见[证据子集](evidence/restore-held-contract-2026-11-14/README.md)。本契约不启用新的 CLI/API。

## 1. 用户结果与现行差异

完整备份经真实管理apply复制到原路径，发布后仍持久隔离；重启、pure_only、旧Run、后台及rebuild均不能进入普通业务栈。用户可专用只读review、同批resume、合法未发布abort。成功只表示“已导入、仍隔离、可复核”。
C切片在B交付后另审release、受管v2普通准入与完整旧Run恢复；B没有release事件/API，不默批`new_runs_only`，不结清PRD§8.5恢复服务义务。
现有`restore-plan`继续只读；当前公共`restore_backup`的直接copy/rename旁路必须收为受管私有复制能力，或让公共API进入本协议；仅改CLI不构成交付。
七DATA/SUM依赖继承fix `0785ae91ce75cd7a45201886423c5d54a18bf949`、integration `016910970d381a66ab12a94a5c88080a8cbf7437`及最小交付包；相同blob只绑定身份，受影响路径改变才重审差异，不重做另一debug线。

## 2. 支持输入与作者选型

只支持现有已校验完整备份、标准根`events.db`、保存的原canonical路径及其已核别名；不跨路径搬迁。源路径按备份保存的词法canonical值比较，不重新resolve已变化/不存在的活源。backup、target、staging、registry不得互为包含路径。
保留现行输入兼容：凡现有真实完整verify可接受的合法机制v0/legacy、v1、v2，均可导入held并专用只读复核；合法v1只报告migration_required，不迁移/洗白或因普通业务拒绝规则拒绝导入。未来/损坏机制、payload/记忆版本及缺失材料仍按真实解析器拒绝。原progress不复制/补造；普通业务继续held，解除后v1及无合法completed的v2普通准入仍由C另审，不在B偷放行。
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
唯一导入链为`reserved → stage_owned → sealed_hold → publish_intent → published_hold`；仅从尚无publish_intent的reserved/stage_owned/sealed_hold进入`abort_intent → aborted`。abort_intent是不可逆的本批终结意图，其后所有管理入口只续清理/终结，不再copy/seal/publish；各事实最多一条，同请求返回原事实。B无released分支。
`reserved`绑定registry/batch、同一backup/manifest/plan、双向copy_inventory的**完整规范本体及摘要**（路径/类型/大小/hash与派生父目录）、原目标absent/empty证明、StablePlacement及唯一同父stage名；本体用于无原备份时辨认合法复制/清理范围，不只存不可逆hash。`stage_owned`在首个stage内文件写前绑定实际目录S/父P、reserved摘要、token精确正式/临时相对路径及可重算的规范token字节/hash、origin精确正式/临时相对路径。token仅含registry/batch/reserved摘要/S/P，不引用stage_owned自身hash，避免自引用。

`sealed_hold`绑定S/D为未来final实体及§5完整Binding；`publish_intent`引用同一sealed、预期origin字节、StablePlacement与E→S映射；`published_hold`引用同一intent及最终核对。`abort_intent`记录前一阶段、从未进入发布的事实及完整精确CleanupPlan本体；`aborted`引用原abort_intent和清理结果，不删除权威。上述都是管理控制事实，不写业务events，也不授予工具副作用许可。

### 3.2 ScopeDescriptor与别名

StablePlacement记录canonical路径段、**final父目录及其向上的稳定OS目录句柄身份链**；父目录尚不存在时记录最近既有祖先及待出现的父后缀，reserved后仅创建这些必要目录，stage_owned再固定实际父P和新增父链。OriginalTarget独立记`absent`或`empty(E)`；E是原空root实体，仅作发布前干净证明，**不放进永远不变的稳定祖先链**。原生大小写/路径比较复用pathkeys语义，不凭字符串前缀判包含，不加路径深度上限。
普通打开与reservation都重开核稳定祖先/P：沿共享既有目录身份映射剩余段，判相等/祖先/后代；root不存在也按“稳定祖先实体＋后缀”识别同一位置。published按稳定P＋final名字及S识别别名/后代；不能把E变化当成任意祖先置换，也不能因此跳过P/祖先检查。别名不能唯一对应、真正稳定祖先置换或身份不可取得即拒绝/过期。
所有未aborted批次的final root和本批staging范围均保留，互相包含/相同、普通main落入任一范围或实体等于held DB都拒绝；`aborted`只解除已证明无恢复根的预约，不许可任何残留origin。普通custom.db/后代DB与标准main同受范围裁决。
恢复main及材料要求真实普通文件、无symlink/junction且main硬链接数为1；其OS实体身份从不截断句柄取得，POSIX至少设备＋inode、Windows至少卷＋file identity，路径hash不是身份。stage_owned先固定S；sealed及intent在rename前绑定同一S/D为**未来final**，不把E当final。empty分支P4只允许rmdir原E后stage(S)→final(S)；absent分支final原本缺失，直接同映射。intent后可见状态只准“S/D仍在stage且final为原缺/空，或S/D已在final且stage名缺失”；P/稳定祖先不变，其他实体、两份矛盾或S/D丢失均不授权。published后E仅留历史；同卷rename不能保持S/D即不支持apply。

## 4. 组合access与写前线性化

现有`DatabaseAccess`只按canonical路径hash加锁，**尚未提供实体身份锁**。本提案保留其namespace句柄，并增加OS文件身份键的shared/exclusive锁，组成同一连接持有的CompositeDatabaseAccess；不能把现有实现称作已闭合。
新取资源的唯一共持顺序：**restore gate → canonical namespace access → entity access（实体存在时）→ registry短读/事务**。全为非阻塞协作锁；registry busy_timeout=0。取锁忙、观察过期、取消/异常保留持久阶段后释放本次资源返回失败/busy，不持锁sleep，不采用旧3次/250ms政策。gate释放后已持namespace/entity者可以继续取其后的registry锁；**不能持namespace/entity反向重拿gate**。
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

新apply先在gate外完整观察/校验，取exclusive gate、最终标准DB namespace exclusive；若main已出现或目标非空，拒绝而不为预约造anchor。核稳定祖先/P、原empty E或absent、backup和registry实体；同事务INSERT reserved＋head CAS，durable后放gate。该固定final namespace exclusive同时是§4.3的批次操作互斥，贯穿apply；P2 main先O_EXCL建零文件、从句柄取entity exclusive，才复制首字节，持到收尾。
普通先行：gate内已固定真实anchor后才释gate；同DB的namespace/entity shared阻挡applyexclusive，custom/后代DB使真实root非空，apply也拒绝。apply先行：durable reserved后即使target/DB都不存在，普通scope裁决也拒绝。reserved后copy不持gate，无关根普通打开可前进。
恢复期间任何DB/根/材料维护都必须遵守同一实体/namespace归属与范围预约；当前namespace锁不能自行发现硬链接。实现必须新增实体锁及scope适配，任一OS无法可信取得/保持上述身份就拒绝受管apply；不能静默退回canonical路径证明。

### 4.3 apply/resume/abort共用的批次操作独占

1. 对已有批次，先在gate外完整读链并结束只读事务，从不可变reserved取**同一固定final namespace键**；调用路径/别名不能另造互斥身份。取exclusive gate→该namespace exclusive→本批现存stage或final D的entity exclusive，核权威head/实际阶段及StablePlacement，再放gate。未取得该独占前，任何stage/control文件写删、seal、intent或rename均禁止；锁忙直接返回，观察“无intent”不构成清理许可。
2. 同一namespace操作独占与已有entity锁持续覆盖长复制、token、核验、清理和durable事实终结；期间不持gate。需要新增D时保持namespace、从新开真实句柄取entity后再写；已有D在stage→final映射不换实体锁。完整registry重观察在gate外完成并结束读事务，随后短INSERT＋head CAS可在既有操作独占下执行，**不反向重拿gate**。
3. 其他根使head过期时，可在仍持操作独占时重新完整观察/结束事务后CAS，或保存已durable阶段、释放并报busy；不能用旧head或解锁后继续删写。同批并发apply/resume/abort只有一个作者；硬退释放OS锁，持久intent保留。close失败仍活的句柄不提前释锁。

## 5. 完整绑定、复制容量与P0–P5

BindingV1含registry/batch、最终canonical root/DB及根/DB实体身份、保存的源路径/已核别名、manifest/双向完整文件清单、关闭自包含DB字节、真实SQLite逻辑/机制/全材料及完整review scope的算法与SHA256、依赖身份。规范编码/域分隔与实际schema测试绑定；不靠计数、maxseq或manifest单向列表。
review scope覆盖全部记忆/正文/状态/来源、所有cursor/消息/原文、summary/来源、intent/result/Receipt/undo与snapshot闭包、审批/grant、全部Run/任务/claim/租约及Run引用并集。B用完整静态副本保留全部本体，不另造供旧/新Run成员执行判断的名单；C在允许写入前必须另定其不可变归属，不能把本稿的hash-only scope外推成C已经有名单本体。
完整材料沿现有三域逐项枚举，包括未引用范围文件；link、未知文件类型、根外引用/缺材料拒绝。origin及本批staging控制token另绑，非业务材料；progress、锁/socket、凭据、日志不复制，不回活源补内容。
容量仍是独立管理提案，逐位置无创建探测实际可用非负整数字节；位置尚不存在就探其已验证最近既有祖先，后续成形再核同一实际卷/身份。未知、探测失败、坏类型均`capacity_unavailable`并在本阶段下一写前停止；不能借同卷或A的2GiB跳过某位置。
- staging采用普通逐字节复制：可用量小于**尚未完整校验文件的声明逻辑字节和＋本阶段尚需新写的精确控制文件字节**即不足；未验证的部分文件不扣减需求，复写需计整文件。此数是明确的应用准入判据，不是实际分配/空间预留保证。
- registry位置：每次准备控制事实前，以该次待提交的完整规范meta/事件payload字节量为已知需求，可用量小于此量（含0）即不足；初始化也计该次已知meta/首记录字节。此保守比较不假设现有空页可免费容纳，也**不含**SQLite页/journal/目录开销，真实commit失败仍保全、不开始后续目标写。
- final父目录位置：有本阶段计划的目录外控制文件时比较其精确字节需求；本协议token/tmp都在stage内，父目录只有mkdir/rmdir/rename等metadata时，可用量为0即先拒绝，正数仅表示未观测到已满，不证明metadata可成功。不另加统一固定余量；mkdir/目录fsync/rename仍逐个检查真实结果。
在每次resume、开始/继续复制、每个控制事实提交及发布前重核相应位置；同卷的多个需求也不被称为已预留。metadata、journal与并发耗用可使预检通过后失败，按真实阶段保全/报告，不吞错误、不自动删别的材料。

1. **P0只读预检**：真实verify、版本/同原路径、完整引用/材料、plan与依赖身份、布局及目标absent/empty、平台身份/durable能力和容量均通过。plan不是许可；apply重验，未reserved无target/staging改动。
2. **P1 durable reserved**：按§4.2发布必要权威并提交唯一预约，含StablePlacement、原E/absent及完整copy_inventory。reserved失败不开始copy；输出/错误都带batch_id，重开可从固定权威按root读回唯一活动批次。
3. **P2同父staging**：在操作独占下排他创建预约stage，保持S/P句柄，**先durable提交stage_owned再写第一个目录内文件**，随后严格按§5.1建立并持久化token才copy。所有复制目的文件/父目录取自reserved完整copy_inventory，私有stage无需另造未登记copy临时文件；需要新临时布局必须先入持久归属，不能随意生成后靠名字清理。逐项验证并形成Binding，stage DB零文件到首字节遵守§4.2。
4. **P3 sealed hold**：重核stage及S/D后durable追加sealed_hold，预绑S/D为未来final，绑定自此不可改；只用stage_owned登记的origin临时/正式路径原子写origin。其规范字节由已提交sealed唯一导出，不反向写入sealed造成hash自引用；临时文件也只准其顺序写前缀/完整字节。token是独立控制见证，可随根保留但backup排除；不得重封同batch洗白变化。
5. **P4 publish intent与发布**：完整重核S/D、材料、容量、稳定P/祖先及target仍原absent或原空E；先durable追加publish_intent，才按其唯一映射rmdir(E，如有)、rename(stage S→final S)、持久化P。P和上层不变；E→缺失→S是已授权目标转换，不能扩成任意祖先替换。intent后不再允许abort，即使未观察到rename。
6. **P5最终核对**：只接受稳定P＋final名字下的S/D、stage名已消失、origin和完整Binding一致，才durable追加published_hold；不拿历史E去核未来final。只报held、batch、指纹及review命令；内容与seal一致时补published不依赖原backup仍可访问。不复制新root重试、不生成业务事件/Receipt、不开服务。

### 5.1 token及控制临时文件的唯一收尾

stage_owned前只准mkdir得到空S，禁止任何目录内文件写。此前硬退重开须先持§4.3操作独占，核reserved范围/稳定父及当前目录句柄，并穷尽证明空；只rmdir该同实体空目录再重建，不递归删。非空不是合法的前stage_owned窗口，保全并拒绝，不能凭basename认领。
stage_owned后，在同一S/P内以O_EXCL、不跟随链接顺序写**其精确登记的token tmp路径**；规范预期字节由事件字段完整重算。token未正式出现而非空现场只允许该一个普通单链接tmp且内容为预期字节的准确前缀（包括空/完整），不能有未知项。该归属来自写前durable stage_owned＋实体＋精确布局/字节，不是名字推断。
1. tmp-write或tmp-fsync前后硬退：resume核上述前缀，删除/重建的仅是这个已证tmp，完整重写→fsync；或abort先按§7 durable固定其清理计划再删。无tmp且S仍空时，按同一已提交stage_owned重新开始token写，不更换S/重记归属。
2. rename前硬退沿步骤1；rename后只有正式token、且字节精确相同，则完成S目录fsync；dir-fsync前后硬退同样重核并幂等fsync。正式与tmp同时出现、错字节/类型/实体或未知内容均不是可解释窗口，保全，不自动删陌生文件。
3. 仅正式token及目录均持久化后才写copy payload；已有payload而正式token缺失/坏不靠补token洗白。origin在sealed后使用已登记的另一精确tmp布局，按同样的可重算字节/前缀规则收尾；其残留不混入业务材料摘要。每条真实原子窗口仍NOT_RUN。

## 6. 只读review与状态输出

建议显式入口：`data restore-apply BACKUP --data-root ROOT --expect-plan SHA256`、`restore-review --data-root ROOT --batch UUID [--later-backup B2]`、`restore-resume`、`restore-abort`；公共Python API走相同受管协议。具体拼写可在正式契约落库时对照现有parser，plan继续独立只读。
review只接受完整published_hold；按gate→namespace shared→entity shared核原观察，释放gate后持续access打开专用只读静态DB。全链扫描在gate外；固定权威/业务文件均只读，拒绝需要hot-journal恢复或会落临时文件的路径，不实例化EventLog/Store/Loop，不迁移/rebuild或改投影/registry，不调用模型/工具，不消耗审批/claim/租约。
输出完整Binding/scope指纹、全部检查与对象ID/状态/来源引用、未决三态/旧许可/租约、已知忘记/删除与不可核项；默认正文脱敏、哈希/大小可查，显式本地范围参数才显示正文。stdout可由用户保存在root外，工具自身不落报告文件。
较新后续史只接现有完整、已verify、同源路径的只读备份，逐项列出已知变化；坏/换输入使review_complete=false。无输入也明确未知；无论是否提供，`later_forget_delete_history=completeness_unproven`。已知忘记不能因恢复激活，所有普通业务仍held。
review无法穷尽任何必需范围就`review_complete=false`并非零退出；完整也只证明字节/引用/既有语义检查覆盖，不代表用户已人工读完正文，更不产release动作/许可。退出0仅当前管理动作完成；拒绝/失败2，真实crashpoint77；错误包含阶段、batch、现场与下一合法命令。
错误类别须在正式类型/schema/PRD同改并测：held、record_corrupt、binding_mismatch、incompatible、capacity_unavailable、durability_unavailable、maintenance_busy、restore_failed；未知异常保留原失败，不兜底为无历史/已abort。

## 7. resume、abort与durable边界

resume在任何写/删前先取§4.3同一批次操作独占，重读完整当前链。已有abort_intent时**只续同一CleanupPlan和aborted终结**，不得重建copy；无abort_intent才按reserved/stage_owned/token规则续做。seal后内容变化保全，不重封；publish_intent后只按§3.2的同一S/D映射续发布/补published，不能重新检查E为永远祖先或创建替代S/D。两处无实体、矛盾或真正稳定祖先不符均闭锁，不按新库/新batch绕过。
abort的唯一顺序如下；**清理前取得独占，持续至durable终结**：
1. 先按§4.3取得同一固定final namespace操作exclusive及本批entity exclusive，在全链和真实现场核定无publish_intent/无可能已发布，target仍原absent或空E，S/P与stage_owned及§5.1允许的空/token/合法tmp状态一致；无stage_owned仅接受尚未创建或已按§5.1证明全空的stage。任何不明内容先拒绝，尚无任何删除。
2. gate已释放但操作独占持续。构造完整CleanupPlan：仅取已证S内、reserved copy_inventory声明的复制节点/派生目录及stage_owned精确控制布局（含合法部分token/origin tmp），文件记录实际相对路径/类型/实体/大小/hash及可解释控制状态；合法部分copy文件按已知归属冻结其实际内容，不要求它先满足完整文件hash。目录记录实体与原直接子节点全集；无stage_owned而按§5.1证明空的S仅可列同句柄实体的空目录rmdir节点。未知项不纳入也不删除，必须先报错。以短registry事务durable追加abort_intent（含完整本体/前一阶段），成功后才逐项unlink/rmdir，目录后序、S最后；每项删除后，须先通过已核身份的真实目录句柄成功持久化受影响的所在目录，再继续删除其上层目录，最终S的rmdir之后必须成功持久化稳定父P。不为该追加反向重拿gate，不释放操作独占去“让清理更快”。
3. 清理中硬退后resume或重复abort重新取同一操作独占、从原abort_intent读回同一计划：已删节点可缺失；剩余节点及因删除未达durable屏障而重现的节点均须重核，文件必须匹配计划的原类型/实体/大小/hash，目录须是原实体且子节点仅为计划原子节点的子集，不要求已清理目录仍有原完整子集hash；任何计划外新增/变化节点均拒绝。只删同一计划内剩余/合法重现项，重做相应目录持久化屏障；即使S已缺失，也须重做稳定父P的屏障，不能凭路径缺失推定删除已持久化。不能重扫并扩大计划、重新seal或改成apply。清理/校验/flush失败或硬退均保留abort_intent与现场，不得结清；flush失败非零退出，平台无法保证所需durable语义则返回durability_unavailable；下一次仍走本分支。
4. 只有计划全部清完、各受影响目录屏障（含S删除后的稳定父P）全部成功，且再次核定S/控制无残留、稳定P/祖先和原target缺/空后，才能在仍持操作独占时追加durable aborted；registry即使位于另一卷，也不得先结清或释放scope。此追加同样只取顺序靠后的registry、不重拿gate。CAS因其他根head变化而失败可gate外重观察再提交或如实返回待收尾；旧abort_intent持续阻断普通入口。commit后丢回包的重试返回原终结事实，不再次清理新数据。全过程不删authority；任何publish_intent/可能已发布均无abort分支。
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
| B06 | 合法v0/v1/v2真实verify→held/review均保留；未来/坏版本、改manifest/DB/投影/FTS/Receipt/summary来源/未引用材料/原policy均拒绝 | RA14、21、23、29–31；v1不迁移洗白，依赖不变者精确继承 |
| B07 | held review全逻辑/控制/材料前后字节与集合不变，零模型/工具/消费；后续忘记变化列全且不宣称完整 | RA15–16；不用release期望 |
| B08 | 真CLI apply→held→review；两进程apply/abort屏障证明清理前互斥；token四窗口、E/absent→S/D、abort_intent清理硬退→同计划幂等终结 | RA24–25、27–28；stage_owned/publish_intent/abort_intent守卫及删错计划负控 |
| B09 | 每实际位置容量不足/未知与copy/commit/origin/rename阶段ENOSPC/I/O均保全，能力不足明确拒绝 | RA27–28；不借2GiB，不伪造空间预留 |

真实断点映射：P0无目标写；FP-INIT/FP-RESERVED；P2 mkdir后stage_owned前、stage_owned commit前/后、token tmp-write/fsync/rename/dir-fsync及FP-STAGE；P3 FP-SEALED/FP-MARKER四窗口；P4 FP-PUBLISH-INTENT commit前/后、empty E的rmdir与absent无E分支、FP-RENAME；P5 FP-PUBLISHED；另FP-ABORT-INTENT commit前/后、每项清理unlink/rmdir之后、各受影响目录持久化屏障前/后、S最后rmdir之后及其稳定父P屏障前/后、FP-ABORTED commit前/后。逐点os._exit(77)、普通重开负探针与两次同批幂等收尾；目录flush失败/不支持必须维持abort_intent而非aborted，清理删除未达屏障的合法重现节点仍按原CleanupPlan重核/清理/补屏障；全部NOT_RUN。
RA17–20、22、26中的release段、32/38/39与旧new_runs_only期望留C重订；RA33/34旧Run集合/固定资源上限不复制进B。RA40只对账B不冒称完整服务恢复。旧43项与10断点原件保持历史NOT_RUN，不把本表数量相加当新验收数。

## 9. 实施与验收边界

本片先完成纯 `docs(contract):` PR，再另行计划、实施受管导入与入口封口。现有 `restore-plan` 仍只读、`apply_supported=false`；现有低层复制能力不能冒充本契约产品交付。

OS 文件/目录身份与 durable 适配、所有公开/后台/材料写者的归属索引、严格类型/schema、真实子进程屏障与每个故障点均为待实施证明义务。若平台不能兑现本契约顺序，明确拒绝该平台 apply，不能静默降级成路径 hash、吞 flush 错误或宣称已支持。

后续实现必须以真实 CLI/公共 API 完成 §8 的正负控并由第二读者复审，绑定最终完整提交和原生平台 CI。合法 v1 的 held 导入兼容、任意数量全空骨架、删除目录屏障先于 aborted 等条款不得以实现细节名义缩减。所有产品验收在有真实证据前继续标记 NOT_RUN；文字复审和低磁盘 CI 均不能替代。

C 切片另行定义显式解除与完整旧 Run 恢复，不默许 `new_runs_only`，不提前结清 PRD §8.5 的恢复服务义务。
