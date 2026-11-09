export function validateSelector(value) {
  if (!value || !Number.isSafeInteger(value.conversation_id) || !/^[0-9a-f]{32}$/.test(value.turn_id || '') ||
      typeof value.interrupt_id !== 'string' || !Array.isArray(value.options) || value.options.length > 20) throw new Error('订单选择信息不完整，请刷新核查。');
  const seen = new Set();
  for (const order of value.options) {
    if (typeof order.order_id !== 'string' || !order.order_id || seen.has(order.order_id) ||
        typeof order.product_name !== 'string' || !Number.isFinite(order.amount) || order.source !== 'demo') throw new Error('订单卡片信息异常。');
    seen.add(order.order_id);
  }
  return value;
}

export function renderOrderSelector(container, selector, context) {
  container.replaceChildren();
  const title = document.createElement('p'); title.className = 'order-selector-title';
  title.textContent = selector.uncertain ? '选单结果待核查，请先核查原请求。' : '请选择要办理退款或售后的订单';
  container.append(title);
  const list = document.createElement('div'); list.className = 'order-card-list';
  for (const order of selector.options) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'order-card';
    button.dataset.orderId = order.order_id;
    const name = document.createElement('strong'); name.textContent = order.product_name;
    const id = document.createElement('span'); id.textContent = `订单 ${order.order_id} · ${order.status}`;
    const price = document.createElement('span'); price.textContent = `¥${order.amount.toFixed(2)} · 演示订单`;
    button.append(name, id, price);
    button.classList.toggle('selected', selector.selected_order === order.order_id);
    button.disabled = context.isBusy() || Boolean(selector.selected_order) || Boolean(selector.uncertain);
    button.addEventListener('click', () => context.select(order.order_id));
    list.append(button);
  }
  container.append(list);
  const controls = document.createElement('div'); controls.className = 'order-selector-controls';
  if (selector.uncertain) {
    const check = document.createElement('button'); check.type = 'button'; check.textContent = '核查选单结果';
    check.disabled = context.isBusy(); check.addEventListener('click', context.check); controls.append(check);
  } else {
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = '取消本次选择';
    cancel.disabled = context.isBusy() || Boolean(selector.selected_order); cancel.addEventListener('click', context.cancel); controls.append(cancel);
  }
  container.append(controls);
}
