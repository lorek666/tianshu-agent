# 恢复 B 本地正式 merge 对象独立身份绑定

**结论：PASS_LOCAL_MERGE_OBJECT_BINDING**

本轮范围内无身份阻断。仅绑定已存在的本地对象与既审候选，不是CI绿色、push、进一步merge或恢复B产品批准。main自身原生CI红色仍是推进阻断。

## 正式对象

- Commit：`277e82e265b54e0e8fe1b2fc1a99d747e95306d5`。
- Tree：`c9f8542dd369138d0f8aa3a81ce7ae826cd3fc1b`。
- Parents按序：`3e9da0bd63399fc86488018605138b54b22fb427`、`b450c339d546ae2007ed9754e70de0108c4b86c4`。
- 使用禁用替代对象的只读 `git --no-replace-objects show -s` 与 `ls-tree -r -z` 独立取得；没有借作者 formal-object facts 作证明。

## 1127个blob身份

正式对象的精确路径集合为1127，全部为100644 blob，无symlink或gitlink。原冻结manifest的SHA256为 `2df189817cda78c20d6a1c3927e9d17b4e544634aa92b459cb408f70462bf31f`。正式对象所有blob ID均精确等于该manifest，但唯一 KNOWLEDGE 路径替换为已审事实overlay：

- 路径：`docs/KNOWLEDGE.md`。
- SHA256：`5b15f0e6abd31c47aa39f06f6546bb085460ec0b45540d5176acfd72e437071a`；347108 bytes。
- Git blob：`b79bb2110f81db7c5f7f829a02dbfe1381a51f89`。

此外实际执行1127次只读 `git show COMMIT:PATH`，逐件重算size、SHA256及Git blob ID，全部与manifest加唯一overlay一致（四线程、3.55s）。这只是对象身份核对，没有重审31份未变正文/契约、重做main净32文稿差异或运行产品行为。

## 继承的本机门与文稿delta

上一轮独立包 `/tmp/restore-b-postgate-doc-local-binding-20261114-06y0yx3a` 的三件固定哈希及29文件/2236033字节的精确集合、全部hash和0444/0555权限再次核验通过；原包未改。其范围是原冻结候选本机门与事后KN三段事实追加。31份未变正文、契约和main产品零delta继承原已审结论。

**本机门覆盖事前冻结manifest2df18981，记录3186 passed / 8 skipped（524.89s）、lock/full/ruff均exit0；正式对象等于该冻结内容加已审单文稿事实delta。正式对象本身没有因此获得“已重跑完整门”或“已跑新原生CI”的标签。**

main b450自身push run43567298082 attempt1为failure，属于上一轮原件核验事实；不能以本次对象身份PASS消账。原146首红、issues145/147未修或根因待查、B未授权及全部NOT_RUN边界保持。

## 证据与限制

原始对象metadata/tree与manifest、KN副本随包保存；facts.json记录来源hash，index.json覆盖本包除自身外全部文件。只读本地Git对象，未写live、refs或远端，未运行full、云端、CI或产品探针。当前工作区是否clean与远端是否包含该对象未在本轮核验，不构成这里的身份保证。
