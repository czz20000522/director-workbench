import React from 'react';
import { reportClientError } from './clientErrorReporter';

/**
 * 单个面板的兜底。
 *
 * 全局错误边界一旦触发就是整页白屏，一个工具里的渲染小毛病会连带把首页也带走。
 * 这里把工具区域单独包起来：崩了只影响这一块，还能重试或返回工具台；异常照样上报。
 */
export default class PanelErrorBoundary extends React.Component<
  React.PropsWithChildren<{ label: string; onBack?: () => void; backText?: string }>,
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error(`Director Workbench「${this.props.label}」渲染异常`, error, info.componentStack);
    reportClientError({
      message: `${this.props.label}：${error.message || '渲染异常'}`,
      stack: error.stack,
      component_stack: info.componentStack || '',
      kind: 'render',
    });
  }

  reset = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    return <div className="sf-center" role="alert">
      <h2>{this.props.label}暂时出错</h2>
      <p className="sf-fine">其他页面不受影响；这次异常已经记录到本机日志（logs/client-errors.log）。</p>
      <div className="sf-step-actions">
        <button className="sf-secondary" type="button" onClick={this.reset}>重试</button>
        {this.props.onBack && <button className="sf-primary" type="button" onClick={this.props.onBack}>{this.props.backText || '返回工具台'}</button>}
      </div>
    </div>;
  }
}
