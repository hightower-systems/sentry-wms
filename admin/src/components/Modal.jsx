export default function Modal({ title, onClose, children, footer, size }) {
  const className = size ? `modal modal-${size}` : 'modal';
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={className} onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>{title}</h2>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer">{footer}</div>}
      </div>
    </div>
  );
}
