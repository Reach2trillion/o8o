# Add project specific ProGuard rules here.
# You can control the set of applied configuration files using the
# proguardFiles setting in build.gradle.
#
# For more details, see
#   http://developer.android.com/guide/developing/tools/proguard.html

# Release builds are not minified (minifyEnabled false). If minification is turned on, keep what the
# ABJ code reaches by reflection: AbjNativePlugin.getInfo() looks Firebase up by name.
-keep class com.google.firebase.FirebaseApp {
    public static java.util.List getApps(android.content.Context);
}

# Uncomment this to preserve the line number information for
# debugging stack traces.
#-keepattributes SourceFile,LineNumberTable

# If you keep the line number information, uncomment this to
# hide the original source file name.
#-renamesourcefileattribute SourceFile
