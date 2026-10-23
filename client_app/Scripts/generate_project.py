#!/usr/bin/env python3
"""Generate the checked-in Xcode project without third-party build tooling."""
from pathlib import Path
import hashlib
import json
import plistlib

root = Path(__file__).resolve().parents[1]
objects = {}
def ident(name): return hashlib.sha256(name.encode()).hexdigest()[:24].upper()
def add(name, body):
    key = ident(name)
    objects[key] = body
    return key
def quote(value): return json.dumps(str(value))
def array(values): return '(' + ', '.join(values) + ',)' if values else '()'
def settings(values): return '{' + ''.join(f'{key} = {quote(value)};' for key, value in values.items()) + '}'

def plist(name, values):
    (root / name).write_bytes(plistlib.dumps(values, sort_keys=False))
base = dict(CFBundleDevelopmentRegion='en', CFBundleExecutable='$(EXECUTABLE_NAME)', CFBundleIdentifier='$(PRODUCT_BUNDLE_IDENTIFIER)', CFBundleInfoDictionaryVersion='6.0', CFBundleName='$(PRODUCT_NAME)', CFBundleShortVersionString='$(MARKETING_VERSION)', CFBundleVersion='$(CURRENT_PROJECT_VERSION)', ITSAppUsesNonExemptEncryption=False, RecipeAppGroup='$(ERECIPE_APP_GROUP)', RecipeKeychainGroup='$(ERECIPE_KEYCHAIN_GROUP)')
plist('App/Info.plist', dict(base, CFBundleDisplayName='eRecipe', CFBundlePackageType='APPL', LSRequiresIPhoneOS=True, UILaunchScreen={}, UIApplicationSceneManifest={'UIApplicationSupportsMultipleScenes': False}, CFBundleURLTypes=[{'CFBundleURLName': 'eRecipe enrollment', 'CFBundleURLSchemes': ['erecipe']}], UISupportedInterfaceOrientations=['UIInterfaceOrientationPortrait', 'UIInterfaceOrientationPortraitUpsideDown', 'UIInterfaceOrientationLandscapeLeft', 'UIInterfaceOrientationLandscapeRight']))
# Match a supported representation explicitly. Safari's Reader PDF provider can
# advertise com.adobe.pdf without public.file-url, alongside other representations.
# The loader validates exact combinations and sizes after the extension opens.
share_activation_rule = '''
extensionItems.@sum.attachments.@count > 0 AND
extensionItems.@sum.attachments.@count <= 6 AND
SUBQUERY(extensionItems, $item,
    SUBQUERY($item.attachments, $attachment,
        ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "com.adobe.pdf" OR
        ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "public.url" OR
        ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "public.plain-text" OR
        ANY $attachment.registeredTypeIdentifiers UTI-CONFORMS-TO "public.image"
    ).@count == $item.attachments.@count
).@count == extensionItems.@count
'''.strip()
plist('ShareExtension/Info.plist', dict(base, CFBundleDisplayName='eRecipe', CFBundlePackageType='XPC!', NSExtension={'NSExtensionPointIdentifier': 'com.apple.share-services', 'NSExtensionPrincipalClass': '$(PRODUCT_MODULE_NAME).ShareViewController', 'NSExtensionAttributes': {'NSExtensionActivationRule': share_activation_rule}}))
for folder in ['App', 'ShareExtension']:
    plist(f'{folder}/eRecipe.entitlements', {'com.apple.security.application-groups': ['$(ERECIPE_APP_GROUP)'], 'keychain-access-groups': ['$(ERECIPE_KEYCHAIN_GROUP)']})

refs = {}
for path in sorted(root.rglob('*')):
    if any(part.startswith('.') for part in path.relative_to(root).parts): continue
    if any(parent.suffix == '.xcassets' for parent in path.parents): continue
    if path.suffix not in ['.swift', '.json', '.png', '.xcconfig', '.plist', '.entitlements', '.xcassets', '.xcprivacy']: continue
    if path.parts[len(root.parts)] in ['Tests', 'UITests', 'Scripts'] or path.name == 'Package.swift': continue
    rel = path.relative_to(root).as_posix()
    if rel == 'Config/Local.xcconfig': continue  # Optional include, never a machine-dependent project reference.
    kind = {'.swift':'sourcecode.swift', '.json':'text.json', '.png':'image.png', '.xcconfig':'text.xcconfig', '.plist':'text.plist.xml', '.entitlements':'text.plist.entitlements', '.xcassets':'folder.assetcatalog', '.xcprivacy':'text.xml'}[path.suffix]
    refs[rel] = add('file:' + rel, f'{{isa = PBXFileReference; lastKnownFileType = {kind}; path = {quote(rel)}; sourceTree = "<group>";}}')
products = {}
for name, extension, kind in [('eRecipe', 'app', 'wrapper.application'), ('ShareExtension', 'appex', 'wrapper.app-extension'), ('eRecipeUITests', 'xctest', 'wrapper.cfbundle')]:
    products[name] = add('product:' + name, f'{{isa = PBXFileReference; explicitFileType = {kind}; path = {name}.{extension}; sourceTree = BUILT_PRODUCTS_DIR;}}')
product_group = add('products', '{isa = PBXGroup; name = Products; children = ' + array(products.values()) + '; sourceTree = "<group>";}')
ui_ref = add('uitestfile', '{isa = PBXFileReference; lastKnownFileType = sourcecode.swift; path = UITests/FlowTests.swift; sourceTree = "<group>";}')
main_group = add('main', '{isa = PBXGroup; children = ' + array([*refs.values(), ui_ref, product_group]) + '; sourceTree = "<group>";}')

def configs(name, values):
    config_ids=[]
    for mode in ['Debug', 'Release']:
        opts = dict(values)
        opts.update(SWIFT_OPTIMIZATION_LEVEL='-Onone' if mode == 'Debug' else '-O', SWIFT_ACTIVE_COMPILATION_CONDITIONS='DEBUG' if mode == 'Debug' else '', DEBUG_INFORMATION_FORMAT='dwarf' if mode == 'Debug' else 'dwarf-with-dsym')
        config_ids.append(add(name+mode, '{isa = XCBuildConfiguration; baseConfigurationReference = '+refs['Config/Signing.xcconfig']+'; buildSettings = '+settings(opts)+'; name = '+mode+';}'))
    return add(name+'configs', '{isa = XCConfigurationList; buildConfigurations = '+array(config_ids)+'; defaultConfigurationIsVisible = 0; defaultConfigurationName = Release;}')

project_configs = configs('project', dict(SWIFT_VERSION='5.0', IPHONEOS_DEPLOYMENT_TARGET='17.0', SDKROOT='iphoneos', CLANG_ENABLE_MODULES='YES', ENABLE_USER_SCRIPT_SANDBOXING='YES', TARGETED_DEVICE_FAMILY='1,2', CODE_SIGN_STYLE='Automatic'))

def buildfile(ref, suffix): return add('build:'+suffix+ref, '{isa = PBXBuildFile; fileRef = '+ref+';}')
def phase(name, kind, files): return add(name, '{isa = '+kind+'; buildActionMask = 2147483647; files = '+array(files)+'; runOnlyForDeploymentPostprocessing = 0;}')
target_ids = {name:ident('target:'+name) for name in products}
for name in products:
    ui = name == 'eRecipeUITests'
    extension = name == 'ShareExtension'
    folder = 'ShareExtension' if extension else 'App'
    sources = [ui_ref] if ui else [ref for path, ref in refs.items() if path.endswith('.swift') and (path.startswith(folder+'/') or path.startswith('Shared/'))]
    resources = [] if ui else [ref for path, ref in refs.items() if path.startswith('Fixtures/') or path == 'PrivacyInfo.xcprivacy' or (path.startswith(folder+'/') and path.endswith('.xcassets'))]
    phases = [phase(name+'sources','PBXSourcesBuildPhase',[buildfile(ref,name) for ref in sources]), phase(name+'resources','PBXResourcesBuildPhase',[buildfile(ref,name) for ref in resources]), phase(name+'frameworks','PBXFrameworksBuildPhase',[])]
    dependencies = []
    if name in ['eRecipe', 'eRecipeUITests']:
        depname = 'ShareExtension' if name == 'eRecipe' else 'eRecipe'
        proxy = add(name+'proxy', '{isa = PBXContainerItemProxy; containerPortal = '+ident('project')+'; proxyType = 1; remoteGlobalIDString = '+target_ids[depname]+'; remoteInfo = '+depname+';}')
        dependencies.append(add(name+'dep','{isa = PBXTargetDependency; target = '+target_ids[depname]+'; targetProxy = '+proxy+';}'))
    if name == 'eRecipe':
        embed = add('embedfile','{isa = PBXBuildFile; fileRef = '+products['ShareExtension']+'; settings = {ATTRIBUTES = (RemoveHeadersOnCopy,);};}')
        phases.append(add('embedphase','{isa = PBXCopyFilesBuildPhase; buildActionMask = 2147483647; dstPath = ""; dstSubfolderSpec = 13; files = ('+embed+',); name = "Embed App Extensions"; runOnlyForDeploymentPostprocessing = 0;}'))
    values = dict(PRODUCT_NAME=name, PRODUCT_BUNDLE_IDENTIFIER='$(ERECIPE_BUNDLE_PREFIX)'+('.share' if extension else '.uitests' if ui else ''), OTHER_LDFLAGS='$(inherited) -lsqlite3', GENERATE_INFOPLIST_FILE='YES' if ui else 'NO', SWIFT_EMIT_LOC_STRINGS='YES')
    if ui: values.update(TEST_TARGET_NAME='eRecipe')
    else: values.update(INFOPLIST_FILE=folder+'/Info.plist', CODE_SIGN_ENTITLEMENTS=folder+'/eRecipe.entitlements')
    if extension: values.update(APPLICATION_EXTENSION_API_ONLY='YES', SKIP_INSTALL='YES')
    elif not ui: values.update(ASSETCATALOG_COMPILER_APPICON_NAME='AppIcon')
    config = configs(name, values)
    product_type = 'com.apple.product-type.bundle.ui-testing' if ui else 'com.apple.product-type.app-extension' if extension else 'com.apple.product-type.application'
    add('target:'+name, '{isa = PBXNativeTarget; buildConfigurationList = '+config+'; buildPhases = '+array(phases)+'; buildRules = (); dependencies = '+array(dependencies)+'; name = '+name+'; productName = '+name+'; productReference = '+products[name]+'; productType = '+quote(product_type)+';}')
add('project','{isa = PBXProject; attributes = {LastUpgradeCheck = 2700;}; buildConfigurationList = '+project_configs+'; compatibilityVersion = "Xcode 14.0"; developmentRegion = en; hasScannedForEncodings = 0; knownRegions = (en, Base,); mainGroup = '+main_group+'; productRefGroup = '+product_group+'; projectDirPath = ""; projectRoot = ""; targets = '+array(target_ids.values())+';}')
project = root/'eRecipe.xcodeproj'
project.mkdir(exist_ok=True)
(project/'project.pbxproj').write_text('// !$*UTF8*$!\n{archiveVersion = 1; classes = {}; objectVersion = 56; objects = {\n'+'\n'.join(f'{key} = {body};' for key,body in objects.items())+'\n}; rootObject = '+ident('project')+';}\n')
schemes=project/'xcshareddata/xcschemes'
schemes.mkdir(parents=True,exist_ok=True)
def reference(name): return f'<BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target_ids[name]}" BuildableName="{name}.{"xctest" if name == "eRecipeUITests" else "app"}" BlueprintName="{name}" ReferencedContainer="container:eRecipe.xcodeproj"/>'
for name, demo in [('eRecipe',False),('eRecipe Demo',True)]:
    (schemes/(name+'.xcscheme')).write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="2700" version="1.3">
<BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES"><BuildActionEntries><BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">{reference('eRecipe')}</BuildActionEntry></BuildActionEntries></BuildAction>
<TestAction buildConfiguration="Debug" shouldUseLaunchSchemeArgsEnv="YES"><Testables><TestableReference skipped="NO">{reference('eRecipeUITests')}</TestableReference></Testables></TestAction>
<LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.IDEFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference('eRecipe')}</BuildableProductRunnable>{'<CommandLineArguments><CommandLineArgument argument="--demo" isEnabled="YES"/></CommandLineArguments>' if demo else ''}</LaunchAction>
<ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES"><BuildableProductRunnable runnableDebuggingMode="0">{reference('eRecipe')}</BuildableProductRunnable></ProfileAction>
<AnalyzeAction buildConfiguration="Debug"/><ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES"/>
</Scheme>''')
print('Generated', project)
