import { test, expect } from '@playwright/test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('directory review → fix preview → manual edit → recheck → history and report',async({page})=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'review-browser-'));
  const original='def append(x, items=[]):\n    items.append(x)\n    return items\n';
  await fs.writeFile(path.join(root,'app.py'),original);
  await fs.writeFile(path.join(root,'clean.py'),'def total(values):\n    return sum(values)\n');
  await fs.writeFile(path.join(root,'other.py'),'x=1\n');
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  try{
    await page.goto('/');
    await page.getByLabel('项目路径').fill(root);
    await page.getByRole('button',{name:'加载项目',exact:true}).click();
    await expect(page.getByRole('checkbox',{name:'选择 app.py'})).toBeChecked();
    await page.getByRole('button',{name:'开始审查',exact:true}).click();
    const issue=page.getByRole('button').filter({hasText:'参数 items 使用了可变默认值'});
    await expect(issue).toBeVisible();
    await expect(page.locator('.monaco-editor').first()).toBeVisible();
    await expect(page.locator('.finding-line').first()).toBeAttached();
    await expect(page.getByText('已证实',{exact:true})).toBeVisible();
    await page.screenshot({path:'test-results/workbench-desktop.png',fullPage:true});
    await page.getByRole('button',{name:'追问',exact:true}).click();
    await page.getByLabel('问题追问').fill('解释这个问题');
    await page.locator('.chat-content form button').click();
    await expect(page.locator('.chat-question')).toHaveText('解释这个问题');
    await expect(page.locator('.chat-content .markdown')).toContainText('本次分析完成');
    await page.getByRole('button',{name:'问题详情',exact:true}).click();
    await page.getByRole('button',{name:'查看修复预览'}).click();
    const dialog=page.getByRole('dialog',{name:'修复预览'});
    await expect(dialog).toBeVisible();
    expect(await fs.readFile(path.join(root,'app.py'),'utf8')).toBe(original);
    await fs.writeFile(path.join(root,'app.py'),'def append(x, items=None):\n    if items is None:\n        items=[]\n    items.append(x)\n    return items\n');
    await dialog.getByRole('button',{name:'已手动修改，复查'}).click();
    await expect(page.locator('.finding-status.resolved')).toHaveText('已解决');
    await expect(page.getByText('原规则在对应作用域重新检查，问题已消失。')).toBeVisible();
    await page.reload();
    await expect(page.locator('.finding-status.resolved')).toHaveText('已解决');
    const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'导出 Markdown 报告'}).click()]);
    const report=await fs.readFile((await download.path())!,'utf8');expect(report).toContain('resolved');expect(report).toContain('app.py');
    await page.getByRole('button',{name:'审查历史',exact:true}).click();
    await expect(page.locator('.history-item')).toHaveCount(2);
    await page.locator('.history-item').last().click();
    await expect(page.locator('.finding-status').first()).toHaveText('待处理');
    await expect(page.getByText('文件已变化或移除，当前显示审查时的快照。')).toBeVisible();
    expect(errors).toEqual([]);
  }finally{await fs.rm(root,{recursive:true,force:true});}
});

test('narrow screen tabs remain usable',async({page})=>{
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await page.getByRole('button',{name:'试用演示项目'}).click();
  await page.getByRole('button',{name:'文件',exact:true}).click();
  await expect(page.getByRole('checkbox',{name:'选择 cart.py'})).toBeVisible();
  await page.getByRole('button',{name:'源码',exact:true}).click();
  await expect(page.locator('.monaco-editor').first()).toBeVisible();
  const widths=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,viewport:window.innerWidth}));
  expect(widths.scroll).toBeLessThanOrEqual(widths.viewport);
  await page.screenshot({path:'test-results/workbench-mobile.png',fullPage:true});
});
