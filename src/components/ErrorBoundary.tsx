import { Component, type ErrorInfo, type ReactNode } from 'react';

type Props = { children: ReactNode };
type State = { error: Error | null };

/** Keeps a render failure recoverable while leaving the original error visible in the console. */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Director Workbench 渲染异常', error, info.componentStack);
  }

  retry = () => this.setState({ error: null });

  render() {
    if (!this.state.error) return this.props.children;
    return <main className="workspace-login" role="alert"><h1>工作台暂时遇到问题</h1><p>页面没有丢失数据。请重试；如果问题持续，请查看浏览器控制台日志。</p><button className="button primary" type="button" onClick={this.retry}>重试</button></main>;
  }
}
