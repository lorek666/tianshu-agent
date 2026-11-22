interface Props {
  criteria: string;
  maxRounds: number;
  disabled: boolean;
  onCriteriaChange: (value: string) => void;
  onMaxRoundsChange: (value: number) => void;
}

export function GoalFields({ criteria, maxRounds, disabled, onCriteriaChange, onMaxRoundsChange }: Props) {
  return <div className="goal-fields">
    <p className="subtle">每轮手动继续。运行成功后，仍需由你按条件验收目标。</p>
    <p className="subtle">使用当前默认模型，未为目标固定模型档案；首版不支持附件和单次模型档案。</p>
    <label>验收条件（每行一条）
      <textarea data-testid="goal-criteria" aria-label="验收条件" rows={3}
        value={criteria} disabled={disabled}
        placeholder={"例如：报告使用中文\n包含可核对的来源"}
        onChange={(event) => onCriteriaChange(event.target.value)} />
    </label>
    <label>最多推进轮次
      <select aria-label="最多推进轮次" value={maxRounds} disabled={disabled}
        onChange={(event) => onMaxRoundsChange(Number(event.target.value))}>
        {Array.from({ length: 10 }, (_, i) => i + 1).map((n) =>
          <option key={n} value={n}>{n} 轮</option>)}
      </select>
    </label>
  </div>;
}
