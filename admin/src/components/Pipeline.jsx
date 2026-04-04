export default function Pipeline({ items }) {
  return (
    <div className="pipeline">
      {items.map((item) => (
        <div key={item.label} className={`pipeline-box ${item.color || ''}`}>
          <div className="pipeline-box-label">{item.label}</div>
          <div className="pipeline-box-count">{item.count ?? 0}</div>
        </div>
      ))}
    </div>
  );
}
